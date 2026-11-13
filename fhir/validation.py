"""Checks on what a FHIR server sends back, before anything reads it (ADR 036).

A record tool turns resources into lines a clinician reads. A malformed answer (an HTML error page
from a proxy, a Bundle of the wrong type, a resource with another id than the one asked for) must
fail loudly here, not become an empty list or a wrong patient further on.

The checks are structural: the envelope (Bundle, CapabilityStatement, the resource read), not
each resource's full profile. They allow what Epic and Oracle Health actually send:

- OperationOutcome entries in a search Bundle (`search.mode: outcome`). Errors among them mean the
  server could not return everything, so the search is reported as possibly incomplete.
- Resources the search asked to include (`search.mode: include`), which the caller ignores.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fhir.errors import FhirResponseError

JSON_TYPES = ("application/fhir+json", "application/json", "application/json+fhir")


@dataclass(frozen=True)
class Page:
    """One validated page of a search."""

    matches: list[dict]                      # resources of the type searched for
    next_url: str | None
    total: int | None
    problems: list[str] = field(default_factory=list)   # error-level OperationOutcome text: the page may be partial


def body_of(r) -> dict:
    """The JSON object in a response, or FhirResponseError (an HTML page, a truncated body ...)."""
    ctype = ((getattr(r, "headers", None) or {}).get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype and ctype not in JSON_TYPES:
        raise FhirResponseError(f"the server answered with {ctype}, not FHIR JSON")
    try:
        body = r.json()
    except ValueError:
        raise FhirResponseError("the server's answer is not valid JSON") from None
    if not isinstance(body, dict):
        raise FhirResponseError("the server's answer is not a JSON object")
    return body


def check_resource(body: dict, resource_type: str, rid: str) -> dict:
    """A read: the resource of that type and id, nothing else."""
    if body.get("resourceType") != resource_type:
        raise FhirResponseError(f"asked for a {resource_type}, the server returned {_kind(body)}")
    if str(body.get("id", "")) != str(rid):
        raise FhirResponseError(f"asked for {resource_type} {rid[:6]}…, the server returned another id")
    return body


def check_capability(body: dict) -> dict:
    if body.get("resourceType") != "CapabilityStatement":
        raise FhirResponseError(f"the server's /metadata returned {_kind(body)}, not a CapabilityStatement")
    return body


def check_page(body: dict, resource_type: str) -> Page:
    """A search page: a Bundle of type searchset whose matches are all of the type searched for."""
    if body.get("resourceType") != "Bundle":
        raise FhirResponseError(f"a {resource_type} search returned {_kind(body)}, not a Bundle")
    if body.get("type", "searchset") != "searchset":
        raise FhirResponseError(f"a {resource_type} search returned a Bundle of type {body.get('type')!r}")
    total = body.get("total")
    if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
        raise FhirResponseError(f"a {resource_type} search returned an invalid total")
    entries = body.get("entry") or []
    links = body.get("link") or []
    if not isinstance(entries, list) or not isinstance(links, list):
        raise FhirResponseError(f"a {resource_type} search returned a malformed Bundle")

    matches: list[dict] = []
    problems: list[str] = []
    for e in entries:
        res = e.get("resource") if isinstance(e, dict) else None
        if not isinstance(res, dict) or not isinstance(res.get("resourceType"), str):
            raise FhirResponseError(f"a {resource_type} search returned an entry without a resource")
        kind = res["resourceType"]
        mode = (e.get("search") or {}).get("mode")
        if kind == "OperationOutcome":
            problems += _errors(res)
        elif kind == resource_type and mode in (None, "match"):
            matches.append(res)
        elif mode == "include":
            continue                               # asked for with _include; the caller reads matches only
        else:
            raise FhirResponseError(f"a {resource_type} search returned a {kind} as a match")

    nxt = next((link.get("url") for link in links if isinstance(link, dict) and link.get("relation") == "next"), None)
    if nxt is not None and not isinstance(nxt, str):
        raise FhirResponseError(f"a {resource_type} search returned an invalid next link")
    return Page(matches, nxt, total, problems)


def _errors(outcome: dict) -> list[str]:
    return [i.get("diagnostics") or (i.get("details") or {}).get("text") or i.get("code") or "error"
            for i in outcome.get("issue") or [] if isinstance(i, dict) and i.get("severity") in ("error", "fatal")]


def _kind(body: dict) -> str:
    kind = body.get("resourceType")
    return f"a {kind}" if isinstance(kind, str) else "something else"
