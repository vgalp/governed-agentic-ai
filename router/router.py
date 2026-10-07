"""Model router: decide which model answers a request, and what it may see.

OPA makes the decision (policies/routing.rego) from facts only: the kinds of
personal information that were masked (never the values), the labels of the data
tools returned, and whether an external model is available. The decision is
audited on audit.routing.

Whatever OPA returns, this code also enforces:
  1. an external model only ever receives masked text;
  2. a profile with external_allowed: false never uses an external model;
  3. if OPA cannot be reached, the request goes to the local model (fail safe).
"""

import re
from dataclasses import dataclass, field

import requests

from audit.chain import get_audit_chain
from gateway.opa import decide
from llm.providers import is_available
from profiles.loader import Profile

TOKEN_RE = re.compile(r"^\[([A-Z_]+)_\d+\]$")


@dataclass
class RouteDecision:
    target: str                    # "local" or "external"
    model_key: str
    model: dict
    send_masked: bool              # True: the model sees masked text
    reasons: list[str] = field(default_factory=list)


def entity_types(mapping: dict[str, str]) -> list[str]:
    """Kinds of personal information that were masked, from tokens like [PERSON_1]."""
    return sorted({m.group(1) for t in mapping if (m := TOKEN_RE.match(t))})


def context_labels(profile: Profile, tools_used: list[str]) -> list[str]:
    return sorted({profile.tool_data_labels.get(t, "unlabeled") for t in tools_used})


def decide_route(profile: Profile, entities: list[str], labels: list[str]) -> RouteDecision:
    r = profile.routing
    ext_key = r["external_model"]
    external_available = bool(ext_key) and is_available(profile.models[ext_key])
    local = RouteDecision("local", r["local_model"], profile.local_model,
                          send_masked=r["local_input"] == "masked")
    try:
        result = decide(profile, "routing/decision", {
            "profile": profile.name,
            "entity_types": entities,
            "context_labels": labels,
            "external_available": external_available,
        }) or {}
    except requests.RequestException:
        local.reasons = ["routing policy unreachable: local model (fail safe)"]
        return local

    reasons = list(result.get("reasons", []))
    if result.get("target") != "external":
        local.reasons = reasons or ["routing policy: local model"]
        return local
    # Checks in code, independent of the policy.
    if not r["external_allowed"] or not external_available:
        local.reasons = reasons + ["policy chose external, overridden: profile or model does not allow it"]
        return local
    return RouteDecision("external", ext_key, profile.models[ext_key], send_masked=True, reasons=reasons)


def route(profile: Profile, mapping: dict[str, str], tools_used: list[str],
          request_id: str, agent: str) -> RouteDecision:
    entities = entity_types(mapping)
    labels = context_labels(profile, tools_used)
    d = decide_route(profile, entities, labels)
    get_audit_chain(agent).emit("audit.routing", {
        "request_id": request_id,
        "profile": profile.name,
        "agent": agent,
        "target": d.target,
        "model_key": d.model_key,
        "model": d.model["name"],
        "provider": d.model["provider"],
        "sent_masked": d.send_masked,
        "entity_types": entities,      # kinds only, never values
        "context_labels": labels,
        "reasons": d.reasons,
    })
    return d
