"""What a FHIR server says it supports, checked against what a profile will ask of it (ADR 036).

Every FHIR server publishes a CapabilityStatement at `/metadata`: the resource types it serves and
the search parameters each accepts. A server that ignores a search parameter it does not support
returns *more* than was asked for (FHIR servers may ignore unknown parameters), so a date window
or a status filter the server does not know is silently not applied. The check runs before the
first question (the tool servers at startup, scripts/fhir_check.py) and says so.

The check reports; it never removes a filter from a search. Findings:

    error    a resource type the profile reads is not offered: those records cannot be read
    warning  a search parameter the profile uses is not declared: the server may ignore it
    info     the statement could not be read, or is not FHIR R4

A server's statement is not always complete (some list fewer parameters than they accept), so a
warning is something to confirm against the vendor's documentation, not proof of a problem.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Parameters every FHIR server must handle, or that are result controls rather than filters.
_ALWAYS = {"_id", "_count", "_sort", "_include", "_revinclude", "_elements", "_summary", "_format"}


@dataclass(frozen=True)
class Capabilities:
    fhir_version: str
    software: str
    resources: dict[str, frozenset[str]] = field(default_factory=dict)   # type -> declared search parameters

    @classmethod
    def parse(cls, statement: dict[str, Any]) -> Capabilities:
        resources: dict[str, frozenset[str]] = {}
        for rest in statement.get("rest") or []:
            if rest.get("mode", "server") != "server":
                continue
            for r in rest.get("resource") or []:
                if isinstance(r, dict) and isinstance(r.get("type"), str):
                    names = {p.get("name") for p in r.get("searchParam") or [] if isinstance(p, dict)}
                    resources[r["type"]] = frozenset(n for n in names if isinstance(n, str))
        software = (statement.get("software") or {}).get("name") or "server"
        return cls(str(statement.get("fhirVersion") or "?"), str(software), resources)

    def offers(self, resource_type: str) -> bool:
        return resource_type in self.resources

    def declares(self, resource_type: str, param: str) -> bool:
        """Whether the server lists the parameter (modifiers such as `:exact` stripped). A resource
        that lists no parameters at all is taken to accept what it is sent: nothing to compare."""
        declared = self.resources.get(resource_type)
        base = param.split(":", 1)[0]
        if not declared or base in _ALWAYS:
            return True
        return base in declared or base.split(".", 1)[0] in declared


@dataclass(frozen=True)
class Finding:
    level: str          # error · warning · info
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.message}"


def check_map(caps: Capabilities, fhir_map) -> list[Finding]:
    """What in a profile's FHIR mapping the server does not declare. `fhir_map` is a
    profiles.fhir_map.FhirMap; only its resource types and search parameter names are read."""
    out: list[Finding] = []
    if not caps.fhir_version.startswith("4.0"):
        out.append(Finding("info", f"the server states FHIR {caps.fhir_version}; this mapping is for R4 (4.0.x)"))

    subject = fhir_map.resource
    if not caps.offers(subject):
        out.append(Finding("error", f"{subject} is not offered: patients cannot be found"))
    else:
        for param in _subject_params(fhir_map):
            if not caps.declares(subject, param):
                out.append(Finding("warning", f"{subject} search parameter '{param}' is not declared"))

    today = dt.date(2000, 1, 1)                     # only the parameter names matter here
    for r in fhir_map.records:
        if not caps.offers(r.resource):
            out.append(Finding("error", f"{r.resource} is not offered: '{r.name}' cannot be read"))
            continue
        for param in r.params("x", today):
            if not caps.declares(r.resource, param):
                what = "the date window may not be applied" if param == r.date_param else "it may be ignored"
                out.append(Finding("warning", f"{r.resource} search parameter '{param}' ('{r.name}') is "
                                              f"not declared: {what}"))
    return out


def _subject_params(fhir_map) -> list[str]:
    names = [v for v in (fhir_map.name_search or {}).values() if isinstance(v, str)]
    ident = getattr(fhir_map, "identifier", None)
    if ident:
        names.append(ident["param"])
    return names


def startup_check(client, fhir_map, label: str) -> list[Finding]:
    """For a tool server at startup: read the statement, log each finding, return them. Never
    raises: a server that is down at startup is retried on each question."""
    from fhir.errors import FhirError
    try:
        caps = Capabilities.parse(client.capability())
    except FhirError as e:
        finding = Finding("info", f"capabilities not checked: {e}")
        log.warning("%s: %s", label, finding)
        return [finding]
    findings = check_map(caps, fhir_map)
    for f in findings:
        (log.error if f.level == "error" else log.warning)("%s: %s", label, f)
    return findings
