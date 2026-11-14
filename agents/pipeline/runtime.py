"""What the pipeline needs from the outside world, in one object.

The stages never import a Kafka producer, a model client or the gateway directly: they get them
from the Runtime, so the whole pipeline runs in a test with fakes and nothing else changes.
`build()` makes the real one for the planner process.
"""

from dataclasses import dataclass, field
from typing import Callable

from profiles.loader import Profile


@dataclass
class Runtime:
    profile: Profile
    producer: object  # .produce(topic, key=, value=) and .flush()
    audit: object  # audit.chain.AuditChain: .emit(topic, event), .producer.flush()
    chat: Callable  # llm.providers.chat(model, messages, purpose=...)
    call_tool: Callable  # gateway.gateway.call_tool
    mask: Callable  # privacy.masking.mask(text, profile) -> (masked, mapping)
    opa_decide: Callable  # gateway.opa.decide(profile, path, input) -> dict
    agent_name: str = "planner"
    guards: dict = field(default_factory=dict)  # which models check the question, the answer, the pages
    faithfulness: object = None  # guardrails.faithfulness.FaithfulnessConfig or None
    agent_model: dict | None = None  # the input agent's model (profiles with one)
    sensitive: Callable | None = None  # text -> the sensitive categories this office tracks (ADR 038), or None

    @property
    def decision_category(self) -> str:
        c = self.profile.classifier
        return c.decision_category or c.default_category


def build(profile: Profile, producer, audit, agent_name: str = "planner") -> Runtime:
    """The real runtime: the planner's producer and audit chain, the real model client, gateway,
    masker and policy engine, and the profile's guard models loaded."""
    from gateway.gateway import call_tool
    from gateway.opa import decide
    from guardrails import faithfulness
    from guardrails.classifier import input_engine_name
    from llm.providers import chat
    from privacy.masking import mask

    cfg = faithfulness.load_config(profile)
    agent_model = profile.models[profile.classifier.input_agent] if profile.classifier.input_agent else None
    guards = {
        "question": input_engine_name(profile),
        "answer": profile.classifier.model,
        "judge": cfg.judge["name"] if cfg else None,
        "input_agent": agent_model["name"] if agent_model else None,
    }
    return Runtime(profile, producer, audit, chat, call_tool, mask, decide, agent_name, guards, cfg, agent_model,
                   sensitive_reader(profile))


def sensitive_reader(profile: Profile) -> Callable | None:
    """text -> the sensitive categories this office tracks, or None when the profile tracks none."""
    settings = profile.sensitivity
    if settings is None:
        return None
    detector = settings.detector()
    return lambda text: settings.tracked(detector.of_text(text))
