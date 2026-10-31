"""OpenAI-compatible chat endpoint: any chatbot that speaks the OpenAI chat format can be put behind
the governed pipeline by changing its base URL (ADR 020).

    POST /v1/chat/completions      (the OpenAI request and response shapes)
    GET  /v1/models                (one model: governed-<profile>)

This module only translates. Every message still goes through the full pipeline (rules, guard
classifier, conversation check, masking, roles, routing, answer checks, audit), exactly like /chat.

What a client cannot do through this endpoint:
- **Set the system prompt.** The office's prompt comes from its profile. A client "system" (or
  "developer") message is dropped, never sent to the model, and counted in the reply's `governance`
  field. Otherwise any client could switch off the rules the prompt sets.
- **Send tool calls, images or audio.** Only text messages from "user" and "assistant" are accepted.
  The pipeline's own tools are chosen by the profile, never by the client.
- **Choose the model.** The `model` field is accepted and ignored; the profile's routing decides.

What the client gets:
- The answer, or the office's fixed reply when a check stopped it (a crisis hand-over, a declined
  decision). `finish_reason` is always "stop", never "content_filter": some chat apps hide content
  they are told was filtered, and a crisis hand-over must be shown.
- A `governance` field (ignored by OpenAI clients): the request id for the audit trail, whether a
  check stopped the answer and which one, and which models checked it. No message text.
- With `stream: true`, the same answer as server-sent events. The answer is checked in full before
  any of it is released, so it arrives at once, not word by word.
"""

import json
import time

ACCEPTED_ROLES = ("user", "assistant")
DROPPED_ROLES = ("system", "developer")
MAX_MESSAGES = 41                  # 40 earlier turns (as /chat) and the new message
MAX_CHARS = 4000                   # per message, as /chat


class CompatError(Exception):
    """A request the endpoint will not accept: becomes an OpenAI-style error with this status."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.code = status, code

    def body(self) -> dict:
        return {"error": {"message": str(self), "type": self.code, "param": None, "code": None}}


def text_of(content) -> str:
    """A message's text. OpenAI allows a string or a list of parts; only text parts are accepted."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str):
                kind = p.get("type") if isinstance(p, dict) else type(p).__name__
                raise CompatError(f"Only text content is accepted, not {kind!r}.")
            parts.append(p["text"])
        return "\n".join(parts)
    raise CompatError("Each message needs text content.")


def to_request(body: dict) -> dict:
    """The pipeline request for an OpenAI chat request: the last user message, the earlier turns as
    history (ADR 019), and the caller's user id."""
    if not isinstance(body, dict):
        raise CompatError("The request body must be a JSON object.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CompatError("'messages' must be a non-empty list.")
    if len(messages) > MAX_MESSAGES + 10:          # room for a few system messages
        raise CompatError(f"At most {MAX_MESSAGES} user and assistant messages are accepted.")
    if body.get("tools") or body.get("functions"):
        raise CompatError("Client tools are not accepted: the office's profile decides which tools exist.")
    turns, dropped = [], 0
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role in DROPPED_ROLES:
            dropped += 1
            continue
        if role not in ACCEPTED_ROLES:
            raise CompatError(f"Messages with role {role!r} are not accepted: only user and assistant.")
        text = text_of(m.get("content"))
        if len(text) > MAX_CHARS:
            raise CompatError(f"A message is longer than {MAX_CHARS} characters.")
        turns.append({"role": role, "content": text})
    if len(turns) > MAX_MESSAGES:
        raise CompatError(f"At most {MAX_MESSAGES} user and assistant messages are accepted.")
    if not turns or turns[-1]["role"] != "user" or not turns[-1]["content"].strip():
        raise CompatError("The last message must be a non-empty user message.")
    user = body.get("user")
    return {"message": turns[-1]["content"], "history": turns[:-1],
            "user_id": user if isinstance(user, str) and user.strip() else "openai-client",
            "dropped_system_messages": dropped, "stream": body.get("stream") is True}


def model_id(profile_name: str) -> str:
    return f"governed-{profile_name}"


def governance(response: dict, dropped: int) -> dict:
    """What the client may know about how the answer was produced. No message text."""
    g = response.get("guardrail") or None
    return {
        "request_id": response.get("request_id"),
        "profile": response.get("profile"),
        "stopped": bool(g),
        "stopped_by": ({k: g.get(k) for k in ("stage", "layer", "category") if g.get(k) is not None}
                       if g else None),
        "answered_by": (response.get("route") or {}).get("model"),
        "guards": response.get("guards"),
        "dropped_system_messages": dropped,
    }


def to_completion(response: dict, profile_name: str, dropped: int = 0, created: int | None = None) -> dict:
    """The OpenAI chat.completion for a pipeline response."""
    metrics = response.get("metrics") or {}
    prompt, completion = int(metrics.get("input_tokens") or 0), int(metrics.get("output_tokens") or 0)
    return {
        "id": f"chatcmpl-{response.get('request_id')}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model_id(profile_name),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response.get("answer") or ""},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
        "governance": governance(response, dropped),
    }


def stream_events(completion: dict):
    """The completion as OpenAI streaming chunks (server-sent events), ending with [DONE]."""
    base = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"],
            "model": completion["model"]}
    content = completion["choices"][0]["message"]["content"]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "governance": completion["governance"]},
    ]
    for c in chunks:
        yield f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def models(profile_name: str) -> dict:
    return {"object": "list", "data": [{"id": model_id(profile_name), "object": "model", "created": 0,
                                        "owned_by": "governed-agentic-ai"}]}


def authorized(header: str | None, keys: list[str]) -> bool:
    """With keys configured, the caller must send one of them as a Bearer token. Without keys the
    endpoint is open, which is only for local development (the API logs a warning at startup)."""
    import hmac
    if not keys:
        return True
    if not header or not header.lower().startswith("bearer "):
        return False
    given = header[7:].strip()
    return any(hmac.compare_digest(given, k) for k in keys)
