"""Send a chat to a model, whichever provider runs it.

Providers:
  ollama              a model served by Ollama on this machine
  openai              OpenAI's hosted models
  gemini              Google's hosted Gemini models (through Google's OpenAI-compatible API)
  anthropic           Anthropic's hosted Claude models (Messages API)
  openai_compatible   any other server with the OpenAI chat API: a hosted service (location
                      external) or one on your own network such as vLLM (location local)
  mock                a fixed, deterministic answer, for tests and machines without a GPU

Which provider answers is decided by the router (router/router.py), never by the user.
API keys are read from the environment variable named in the profile (api_key_env)
and are never logged or audited.
"""

import os

import requests

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
TIMEOUT = 120

# Default addresses of the hosted providers. A profile may override base_url,
# for example to use a regional or company endpoint.
BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com/v1",
}
ANTHROPIC_VERSION = "2023-06-01"
HOSTED = set(BASE_URLS)            # always third-party services: location must be external


class ModelUnavailable(Exception):
    """The model could not be called or gave no usable answer. The router then falls back."""


def is_available(model: dict) -> bool:
    """True if the model can be called: an external model needs its API key set."""
    env = model.get("api_key_env")
    return not env or bool(os.environ.get(env))


def _key(model: dict) -> str | None:
    env = model.get("api_key_env")
    if not env:
        return None
    key = os.environ.get(env)
    if not key:
        raise ModelUnavailable(f"environment variable {env} is not set")
    return key


def _base_url(model: dict) -> str:
    return (model.get("base_url") or BASE_URLS[model["provider"]]).rstrip("/")


def _post(url: str, headers: dict, body: dict) -> dict:
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ModelUnavailable(f"could not reach the model: {type(e).__name__}") from e
    if resp.status_code != 200:
        # The status only: error bodies can echo the request, which may hold personal data.
        raise ModelUnavailable(f"model returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise ModelUnavailable("model returned a response that is not JSON") from e


def _openai_chat(url: str, model: dict, messages: list[dict], key: str | None = None) -> str:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _post(url, headers, {
        "model": model["name"],
        "temperature": model.get("temperature", 0),
        "messages": messages,
    })
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ModelUnavailable("model response has no answer text") from e


def _anthropic_chat(model: dict, messages: list[dict]) -> str:
    """Claude's Messages API takes the system prompt separately from the conversation."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    data = _post(_base_url(model) + "/messages", {
        "x-api-key": _key(model),
        "anthropic-version": ANTHROPIC_VERSION,
    }, {
        "model": model["name"],
        "max_tokens": model.get("max_tokens", 1024),
        "temperature": model.get("temperature", 0),
        "system": system,
        "messages": [{"role": m["role"], "content": m["content"]}
                     for m in messages if m["role"] != "system"],
    })
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    if not text:
        raise ModelUnavailable("model response has no answer text")
    return text


def chat(model: dict, messages: list[dict]) -> str:
    provider = model["provider"]
    if provider == "ollama":
        return _openai_chat(OLLAMA_URL, model, messages)
    if provider in {"openai", "gemini"}:
        return _openai_chat(_base_url(model) + "/chat/completions", model, messages, _key(model))
    if provider == "anthropic":
        return _anthropic_chat(model, messages)
    if provider == "openai_compatible":
        return _openai_chat(model["base_url"].rstrip("/") + "/chat/completions", model, messages, _key(model))
    if provider == "mock":
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return model.get("answer", "Here is one small next step: write down the first task. ({chars} characters received)").format(chars=len(last))
    raise ModelUnavailable(f"unknown provider {provider!r}")