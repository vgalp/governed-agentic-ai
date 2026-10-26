"""What each request cost: every model call, its tokens and time, and the price (ADR 018).

The planner starts a meter for each request (`start()`); every model call made while it handles
the request records itself (`record()`): the answering model, the input agent, the model that
prepares a proposed change, Llama Guard and the guard classifier. When the request ends, its
summary goes with the response and into the trace, and the dashboard adds them up.

Privacy: a meter holds counts, times and model names, never text.

Cost: tokens times the model's price in its profile (`price: {input_per_million, output_per_million}`,
in US dollars). A model with no price is counted as $0 if local (the cost of running your own
hardware is not included) and as "no price set" if external, so a missing price is never shown
as free.
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

PURPOSES = ("answer", "proposal", "input_agent", "question_check", "answer_check")


@dataclass
class Call:
    purpose: str                    # one of PURPOSES
    model: str
    provider: str
    location: str                   # local or external
    input_tokens: int | None        # None: the model gives no token count (e.g. the guard classifier)
    output_tokens: int | None
    estimated: bool                 # tokens estimated from text length (the model returned no count)
    ms: int
    ok: bool
    cost_usd: float | None          # None: external model with no price in the profile


@dataclass
class Meter:
    calls: list[Call] = field(default_factory=list)
    toxicity: float | None = None   # highest toxicity score of the model's answer (0 to 1)
    toxicity_model: str | None = None
    started: float = field(default_factory=time.monotonic)

    def summary(self, outcome: str) -> dict:
        llm = [c for c in self.calls if c.input_tokens is not None]
        costs = [c.cost_usd for c in self.calls]
        tokens_in = sum(c.input_tokens or 0 for c in llm)
        tokens_out = sum(c.output_tokens or 0 for c in llm)
        return {
            "outcome": outcome,
            "model_calls": len(self.calls),
            "llm_calls": len(llm),
            "failed_calls": sum(not c.ok for c in self.calls),
            "by_purpose": {p: sum(c.purpose == p for c in self.calls) for p in PURPOSES
                           if any(c.purpose == p for c in self.calls)},
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "tokens_estimated": any(c.estimated for c in llm),
            "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 6),
            "unpriced_models": sorted({c.model for c in self.calls if c.cost_usd is None}),
            "model_ms": sum(c.ms for c in self.calls),
            "total_ms": round((time.monotonic() - self.started) * 1000),
            "toxicity": self.toxicity,
            "toxicity_model": self.toxicity_model,
            "calls": [asdict(c) for c in self.calls],
        }


_meter: ContextVar[Meter | None] = ContextVar("usage_meter", default=None)
_purpose: ContextVar[str | None] = ContextVar("usage_purpose", default=None)


@contextmanager
def purpose(name: str):
    """Calls made inside count as `name` (e.g. the input agent's), whatever the caller passed."""
    if name not in PURPOSES:
        raise ValueError(f"unknown purpose {name!r}")
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def start() -> Meter:
    """A new meter for the request being handled; earlier requests' calls are not counted."""
    m = Meter()
    _meter.set(m)
    return m


def current() -> Meter | None:
    return _meter.get()


def price_of(model: dict, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Cost of one call in US dollars, from the model's price in the profile."""
    price = model.get("price")
    if not price:
        return 0.0 if model.get("location", "local") == "local" else None
    return ((input_tokens or 0) * float(price.get("input_per_million", 0))
            + (output_tokens or 0) * float(price.get("output_per_million", 0))) / 1_000_000


def estimate_tokens(text: str) -> int:
    """About four characters per token for English text: used only when a model returns no count."""
    return max(1, round(len(text) / 4)) if text else 0


def record(purpose: str, model: dict, input_tokens: int | None, output_tokens: int | None,
           ms: float, ok: bool = True, estimated: bool = False) -> None:
    """Add a call to the current request's meter. Outside a request (tests, scripts): nothing."""
    m = _meter.get()
    if m is None:
        return
    purpose = _purpose.get() or purpose
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    cost = price_of(model, input_tokens, output_tokens) if ok else 0.0
    m.calls.append(Call(purpose, model.get("name", "?"), model.get("provider", "?"), model.get("location", "local"),
                        input_tokens, output_tokens, estimated, round(ms), ok, cost))


def outcome_of(guardrail: dict | None, approval: dict | None) -> str:
    """How the request ended, from what was published:
      answered   the assistant answered (a model, or a tool's source text)
      proposed   a change was proposed for a person to approve
      declined   a policy decided before the model: crisis hand-off, forbidden decision, role, tool
      withheld   the model wrote an answer that failed the output checks, so a fixed reply was sent
      failed     something did not work: a safety check or model was unavailable, a change was unclear
    """
    if approval:
        return "proposed"
    if not guardrail:
        return "answered"
    category, stage = guardrail.get("category"), guardrail.get("stage")
    if category in ("guardrail_unavailable", "model_unavailable", "action_unclear"):
        return "failed"
    if stage == "output":
        return "withheld"
    return "declined"


SUCCESS = ("answered", "proposed", "declined")
