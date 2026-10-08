"""Human approval before consequential actions.

The assistant may PROPOSE a change (profiles/<name>/actions.yaml); it never makes one.

  1. A request matching an action's intent pattern becomes a proposal: the local model
     fills in the action's fields from the records it was given, as JSON.
  2. Code checks the proposal: every field present, IDs only from the records the model
     was given, dates that parse. The model's text is never shown or executed as is.
  3. OPA decides whether this role may ask for it and whether the facts are allowed
     (policies/actions.rego). Every allowed change needs a person's approval.
  4. The proposal goes to approvals.requested (Kafka). A person with an approver role
     approves or rejects it (approvals.decided); not approving in time means expired.
  5. The executor agent (agents/executor.py) checks again with OPA and, only if allowed,
     makes the change through the gateway. The result goes to actions.executed. An
     approval by someone who may not approve (wrong role, or their own request) is
     refused, and the request stays open for a real approver.

Every step is audited on audit.approvals and shown in the decision trace. Approvals
hold masked text and IDs only, never names.
"""

import json
import re
import uuid
from datetime import datetime, timedelta

from profiles.loader import Action, Profile

REQUESTED = "approvals.requested"
DECIDED = "approvals.decided"
EXECUTED = "actions.executed"
APPROVAL_TOPICS = [REQUESTED, DECIDED, EXECUTED]

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def detect(profile: Profile, text: str) -> Action | None:
    """The first action whose intent pattern matches the request, if any."""
    for action in profile.actions.values():
        if action.intent.search(text):
            return action
    return None


def extraction_messages(action: Action, notes_text: str, request: str, now: datetime) -> list[dict]:
    """Ask the model to fill in the proposal. It is told it changes nothing."""
    fields = "\n".join(f'- "{name}": {spec["description"]}' for name, spec in action.fields.items())
    system = (
        f"You prepare a proposed change for staff to approve: {action.title.lower()}. "
        f"{action.description} You never make the change yourself, and nothing changes until "
        "a person approves it.\n\n"
        f"Today is {now:%A %Y-%m-%d}. Reply with one JSON object and nothing else, with these fields:\n"
        f"{fields}\n\n"
        "Use only IDs that appear in the notes. If the request is unclear, or the item is not in "
        'the notes, reply {"error": "<short reason>"} instead.\n\n'
        f"Notes:\n{notes_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": request}]


def parse_proposal(raw: str) -> dict | str:
    """The JSON object in the model's reply, or an error message."""
    m = JSON_RE.search(raw or "")
    if not m:
        return "the model did not return a proposal"
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return "the model's proposal was not valid JSON"
    if not isinstance(data, dict):
        return "the model's proposal was not an object"
    if data.get("error"):
        return str(data["error"])[:200]
    return data


def validate(action: Action, proposal: dict, given_ids: list[str]) -> tuple[dict | None, str | None]:
    """Check every field. Returns (arguments for the tool, None) or (None, reason)."""
    args = {}
    for name, spec in action.fields.items():
        value = proposal.get(name)
        if value in (None, ""):
            return None, f"the proposal has no {name}"
        value = str(value).strip()
        if spec.get("pattern") and not spec["pattern"].search(value):
            return None, f"{name} '{value}' does not look right"
        if spec["type"] == "source" and value not in given_ids:
            return None, f"{name} '{value}' is not in the records I was given"
        if spec["type"] == "datetime":
            try:
                value = datetime.fromisoformat(value.replace(" ", "T")).isoformat(timespec="minutes")
            except ValueError:
                return None, f"{name} '{value}' is not a date and time"
        args[name] = value
    return args, None


def facts(action: Action, args: dict, now: datetime) -> dict:
    """What the policy needs about each datetime field: day, hour, and whether it is past."""
    out = {}
    for name, spec in action.fields.items():
        if spec["type"] == "datetime":
            t = datetime.fromisoformat(args[name])
            out[name] = {"weekday": t.weekday(), "hour": t.hour, "minute": t.minute, "in_past": t <= now}
    return out


def summary(action: Action, args: dict, notes: list[dict], masker=None) -> str:
    """One line for the approver: what changes, with the current record masked."""
    by_id = {n["id"]: n for n in notes}
    parts = []
    for name, spec in action.fields.items():
        value = args[name]
        if spec["type"] == "source" and value in by_id:
            text = by_id[value]["text"]
            if masker:
                text = masker(text)
            parts.append(f"{value} ({text.rstrip('.')})")
        elif spec["type"] == "datetime":
            parts.append(f"to {value.replace('T', ' at ')}")
        else:
            parts.append(f"{name} {value}")
    return f"{action.title}: " + " ".join(parts)


def new_approval(profile: Profile, action: Action, args: dict, text: str, request_id: str,
                 requester: str, role: str, approve_roles: list[str], now: datetime) -> dict:
    return {
        "approval_id": f"ap-{uuid.uuid4().hex[:8]}",
        "request_id": request_id,
        "profile": profile.name,
        "action": action.name,
        "title": action.title,
        "args": args,                         # IDs and times only; no names
        "summary": text,                      # masked
        "requester": requester,
        "requester_role": role,
        "approve_roles": list(approve_roles),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=action.expires_minutes)).isoformat(timespec="seconds"),
    }


class ApprovalState:
    """Approvals rebuilt from the three Kafka topics. Kafka is the record; this is a view
    of it, used by the executor (what to do) and the API (what to show)."""

    def __init__(self):
        self.approvals: dict[str, dict] = {}

    def apply(self, topic: str, event: dict) -> dict | None:
        a = self.approvals.get(event.get("approval_id"))
        if topic == REQUESTED:
            self.approvals[event["approval_id"]] = {**event, "status": "pending"}
            return self.approvals[event["approval_id"]]
        if a is None:
            return None
        if topic == DECIDED and a["status"] == "pending":
            a.update(status={"approve": "approved", "reject": "rejected", "expired": "expired"}[event["decision"]],
                     decided_by=event.get("approver"), decided_role=event.get("approver_role"),
                     decided_at=event.get("decided_at"), note=event.get("note"))
        elif topic == EXECUTED and event["status"] == "approval_refused":
            # Someone who may not approve tried to (wrong role, or their own request):
            # the request stays open for a real approver.
            a.update(status="pending", refused=event.get("reasons"), decided_by=None, decided_role=None)
        elif topic == EXECUTED:
            # The final word. It names the approver itself, so the result is right even if
            # the decision events were read in another order.
            a.update(status=event["status"], result=event.get("result"), reasons=event.get("reasons"),
                     decided_by=event.get("approver"), decided_role=event.get("approver_role"))
        return a

    def pending(self) -> list[dict]:
        return [a for a in self.approvals.values() if a["status"] == "pending"]

    def overdue(self, now: datetime) -> list[dict]:
        return [a for a in self.pending() if datetime.fromisoformat(a["expires_at"]) <= now]