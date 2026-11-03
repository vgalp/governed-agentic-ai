"""The state of one request as it moves through the pipeline, and how it ends.

`Request` is filled in by the stages (agents/pipeline/stages.py) in order; each field is set by
the stage named next to it and read by later ones. `Outcome` is a terminal result: the reply to
publish, with what the UI and the audit need to say why. A stage that ends the request returns an
Outcome; otherwise it returns None and the next stage runs.
"""

from dataclasses import dataclass, field

from agents.trace import Trace
from guardrails.rules import GuardrailDecision
from router.router import RouteDecision


@dataclass
class Request:
    raw: dict  # the chat.requests message as received
    request_id: str
    role: str | None
    message: str
    history: list[dict]  # earlier turns (conversation.clean)
    trace: Trace
    # intake
    in_check: GuardrailDecision | None = None
    in_layer: str = "rules"
    override: str | None = None
    # roles
    access: dict | None = None  # {role, role_title, restricted: [data classes not seen]}
    # masking
    masked_message: str = ""
    mapping: dict = field(default_factory=dict)
    masked_history: list[dict] = field(default_factory=list)
    # change requests
    action: object = None  # actions.Action, when the message asks for a change
    # tools
    notes: list[dict] = field(default_factory=list)  # records and pages given to the model
    tools_used: list[str] = field(default_factory=list)  # tools that returned data
    triggered: list[str] = field(default_factory=list)  # tools the question asked for (when)
    direct: tuple | None = None  # (tool, answer text, results) from an answer: direct tool
    denied_for_role: str | None = None
    tool_notes: list[str] = field(default_factory=list)  # for the model: why records were not found
    empty_notes: dict = field(default_factory=dict)  # tool -> note, for tools that returned nothing
    notes_text: str = ""
    # answer
    decision: RouteDecision | None = None
    route_info: dict | None = None
    model_answer: str | None = None
    restored_answer: str = ""
    out_check: GuardrailDecision | None = None
    out_layer: str = "rules"


@dataclass(frozen=True)
class Outcome:
    """How the request ended: what to publish and what to log."""

    answer: str
    sources: list = field(default_factory=list)
    guardrail: dict | None = None  # {stage, layer, category[, engine]} when a check decided
    route_info: dict | None = None
    approval: dict | None = None
    log: str = ""  # one console line
    trace_step: tuple | None = None  # (agent, step, outcome, summary, details) recorded with it
