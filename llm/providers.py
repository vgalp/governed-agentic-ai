"""Send a chat to a model, whichever provider runs it.

Providers:
  ollama              a model served by Ollama on this machine
  openai_compatible   any server with the OpenAI chat API: a hosted service (location
                      external) or one on your own network such as vLLM (location local)
  mock                a fixed, deterministic answer, for tests and machines without a GPU

API keys are read from the environment variable named in the profile (api_key_env)
and are never logged or audited.
"""

import os

import requests

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
TIMEOUT = 120


class ModelUnavailable(Exception):
    pass


def is_available(model: dict) -> bool:
    """True if the model can be called: an external model needs its API key set."""
    env = model.get("api_key_env")
    return not env or bool(os.environ.get(env))


def _openai_chat(url: str, model: dict, messages: list[dict], headers: dict | None = None) -> str:
    resp = requests.post(url, headers=headers or {}, json={
        "model": model["name"],
        "temperature": model.get("temperature", 0),
        "messages": messages,
    }, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def chat(model: dict, messages: list[dict]) -> str:
    provider = model["provider"]
    if provider == "ollama":
        return _openai_chat(OLLAMA_URL, model, messages)
    if provider == "openai_compatible":
        headers = {}
        env = model.get("api_key_env")
        if env:
            key = os.environ.get(env)
            if not key:
                raise ModelUnavailable(f"environment variable {env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        return _openai_chat(model["base_url"].rstrip("/") + "/chat/completions", model, messages, headers)
    if provider == "mock":
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return model.get("answer", "Here is one small next step: write down the first task. ({chars} characters received)").format(chars=len(last))
    raise ModelUnavailable(f"unknown provider {provider!r}")
