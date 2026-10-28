"""Conversations: earlier turns of the same chat, sent with each request (ADR 019).

The pipeline keeps no chat state. A client that wants a conversation sends the earlier turns with
each new message, as `history`: [{"role": "user" | "assistant", "content": "..."}, ...], oldest
first. Three things use them:

1. **Crisis over the conversation.** Risk often builds over several messages: no single one is
   alarming, but together they are. The current message is checked as before; then the recent
   user turns and the current message, joined, are checked again by the same rules and classifier,
   and so is each recent user turn on its own (in a long joined text one alarming message can be
   diluted). If any of these is a crisis, the crisis reply wins. Only crisis is raised this way: whether a
   forbidden decision is being asked for is judged on what is asked now, so one earlier medication
   question does not block the rest of the chat. Once a conversation has shown a crisis sign, every
   later turn gets the crisis reply too: the person should be talking to someone by then.
2. **The model sees the earlier turns,** masked the same way as the message (one mapping for the
   whole conversation, so a name keeps its token across turns).
3. **Nothing else changes:** tools, roles, change proposals and the answer checks use the current
   message only.

History comes from the client, so it is treated like any other input: only the last
MAX_TURNS turns are kept, each cut to MAX_TURN_CHARS characters, and anything malformed is dropped.
"""

from collections.abc import Callable

MAX_TURNS = 8                 # earlier turns kept (user and assistant together)
MAX_TURN_CHARS = 2000         # per turn
USER_TURNS_CHECKED = 4        # earlier user turns joined with the current message for the crisis check
SEP = "\n\n<<<turn>>>\n\n"    # joins turns for masking; never a personal detail, so never masked


def clean(history) -> list[dict]:
    """The usable part of what the client sent: well-formed turns, the most recent MAX_TURNS."""
    if not isinstance(history, list):
        return []
    turns = []
    for t in history:
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant"):
            continue
        content = t.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": t["role"], "content": content[:MAX_TURN_CHARS]})
    return turns[-MAX_TURNS:]


def view(history: list[dict], message: str) -> str | None:
    """The recent user turns and the current message, oldest first. None without earlier user turns."""
    earlier = [t["content"] for t in history if t["role"] == "user"][-USER_TURNS_CHECKED:]
    if not earlier:
        return None
    return "\n".join([*earlier, message])


def views(history: list[dict], message: str) -> list[str]:
    """What the conversation check reads: the joined view, then each recent earlier user turn on its
    own, most recent first. Empty without earlier user turns."""
    joined = view(history, message)
    if joined is None:
        return []
    earlier = [t["content"] for t in history if t["role"] == "user"][-USER_TURNS_CHECKED:]
    return [joined, *reversed(earlier)]


def crisis_in_views(texts: list[str], rules: Callable, classifier: Callable):
    """The first crisis decision among the texts, or None."""
    for text in texts:
        seen = crisis_in_view(text, rules, classifier)
        if seen:
            return seen
    return None


def crisis_in_view(text: str, rules: Callable, classifier: Callable):
    """The crisis decision for the joined view, or None. Rules first (fast), then the classifier.
    `rules` and `classifier` take the text and return a GuardrailDecision."""
    ruled = rules(text)
    if not ruled.allowed and ruled.category == "crisis":
        return ruled
    flagged = classifier(text)
    if not flagged.allowed and flagged.category == "crisis":
        return flagged
    return None


def mask_all(history: list[dict], message: str, mask_fn: Callable) -> tuple[list[dict], str, dict]:
    """Mask the earlier turns and the message together, with one mapping.

    Returns (masked history, masked message, mapping). If the turns cannot be told apart after
    masking (it should not happen), the history is dropped rather than risk sending it unmasked."""
    texts = [t["content"] for t in history] + [message]
    masked, mapping = mask_fn(SEP.join(texts))
    parts = masked.split(SEP)
    if len(parts) != len(texts):
        masked_message, mapping = mask_fn(message)
        return [], masked_message, mapping
    masked_history = [{"role": t["role"], "content": p} for t, p in zip(history, parts[:-1])]
    return masked_history, parts[-1], mapping
