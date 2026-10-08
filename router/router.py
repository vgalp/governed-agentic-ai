"""Model router: decide which model answers a request, and what it may see.

OPA makes the decision (policies/routing.rego) from facts only: the kinds of
personal information that were masked (never the values), the labels of the data
tools returned, and whether an external model is available. The decision is
audited on audit.routing.

Whatever OPA returns, this code also enforces:
  1. an external model only ever receives masked text;
  2. a profile with external_allowed: false never uses an external model;
  3. if OPA cannot be reached, the request goes to the local model (fail safe).

Which external provider (OpenAI, Gemini, Claude, ...) is configured in the profile:
routing.external_model, then routing.fallback in order. The first one with its API key
set is used; if a call fails, the next is tried, and the local model is always last.
The policy decides whether a request may leave at all; the provider makes no difference.
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


def external_chain(profile: Profile) -> list[str]:
    """The profile's external models in the order they are tried."""
    r = profile.routing
    return [r["external_model"], *r["fallback"]] if r["external_model"] else []


def available_externals(profile: Profile) -> list[str]:
    """External models in the chain that can be called now (API key set)."""
    return [k for k in external_chain(profile) if is_available(profile.models[k])]


def local_decision(profile: Profile, reasons: list[str] | None = None) -> RouteDecision:
    r = profile.routing
    return RouteDecision("local", r["local_model"], profile.local_model,
                         send_masked=r["local_input"] == "masked", reasons=list(reasons or []))


def fallbacks(profile: Profile, failed: RouteDecision) -> list[RouteDecision]:
    """What to try, in order, after a call to `failed` did not work: the remaining
    available external models (still masked: the policy already allowed this data out),
    then the local model."""
    if failed.target != "external":
        return []
    chain = available_externals(profile)
    rest = chain[chain.index(failed.model_key) + 1:] if failed.model_key in chain else []
    note = f"{failed.model_key} failed"
    return ([RouteDecision("external", k, profile.models[k], send_masked=True,
                           reasons=[f"{note}: falling back to {k}"]) for k in rest]
            + [local_decision(profile, [f"{note}: falling back to the local model"])])


def decide_route(profile: Profile, entities: list[str], labels: list[str]) -> RouteDecision:
    r = profile.routing
    externals = available_externals(profile)
    external_available = bool(externals)
    local = local_decision(profile)
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
    blocked = sorted(set(labels) & set(r["never_external_labels"]))
    if blocked:
        local.reasons = reasons + [f"policy chose external, overridden: uses {', '.join(blocked)} data"]
        return local
    key = externals[0]
    if key != r["external_model"]:
        reasons.append(f"{r['external_model']} has no API key set: using {key}")
    return RouteDecision("external", key, profile.models[key], send_masked=True, reasons=reasons)


def audit_route(profile: Profile, d: RouteDecision, request_id: str, agent: str,
                role: str | None = None, entities: list[str] | None = None,
                labels: list[str] | None = None, fallback: bool = False) -> None:
    event = {
        "request_id": request_id,
        "profile": profile.name,
        "agent": agent,
        "role": role,
        "target": d.target,
        "model_key": d.model_key,
        "model": d.model["name"],
        "provider": d.model["provider"],
        "sent_masked": d.send_masked,
        "reasons": d.reasons,
    }
    if fallback:
        event["fallback"] = True
    else:
        event["entity_types"] = entities or []      # kinds only, never values
        event["context_labels"] = labels or []
    get_audit_chain(agent).emit("audit.routing", event)


def route(profile: Profile, mapping: dict[str, str], tools_used: list[str],
          request_id: str, agent: str, role: str | None = None) -> RouteDecision:
    entities = entity_types(mapping)
    labels = context_labels(profile, tools_used)
    d = decide_route(profile, entities, labels)
    audit_route(profile, d, request_id, agent, role, entities, labels)
    return d