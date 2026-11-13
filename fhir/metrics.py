"""Per-call metrics for FHIR traffic, in Prometheus text format (ADR 036).

In-process and dependency-free: counters and latency histograms keyed by a small, fixed set of
labels. Each tool server exposes its own registry at `GET /metrics` (mcp_servers/common/serve.py).

Labels never carry patient data. `server` is the host name; `resource` is the FHIR resource type
from the URL path, checked against a pattern (an id, or anything else, becomes "other"), so the
number of series stays bounded whatever the server returns.

    fhir_requests_total{server, resource, outcome}      every HTTP attempt, by outcome
    fhir_request_seconds{server, resource}              latency of attempts that got an answer
    fhir_retries_total{server, reason}                  attempts made again (busy, timeout, ...)
    fhir_rejected_total{server, reason}                 calls refused before sending (breaker, rate, deadline, queue)
    fhir_breaker_open{server}                           1 while the breaker is open or half open
    fhir_cache_total{result}                            per-session cache hits and misses
    fhir_response_invalid_total{server, resource}       answers that failed validation
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from collections import defaultdict

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 45.0, 90.0)
_RESOURCE = re.compile(r"^(?:[A-Z][A-Za-z]{1,63}|metadata)$")
_LABEL_VALUE = re.compile(r"[^A-Za-z0-9_.:-]")

HELP = {
    "fhir_requests_total": ("counter", "HTTP attempts to FHIR servers, by outcome"),
    "fhir_request_seconds": ("histogram", "Latency of FHIR HTTP attempts that got an answer"),
    "fhir_retries_total": ("counter", "FHIR attempts made again, by reason"),
    "fhir_rejected_total": ("counter", "FHIR calls refused before sending, by reason"),
    "fhir_breaker_open": ("gauge", "1 while a server's circuit breaker is open or half open"),
    "fhir_cache_total": ("counter", "Per-session FHIR cache lookups, by result"),
    "fhir_response_invalid_total": ("counter", "FHIR answers that failed validation"),
}


def resource_label(base_url: str, url: str) -> str:
    """The resource type a request is for ('Patient', 'metadata'), or 'other'. Never an id."""
    base = urllib.parse.urlsplit(base_url).path.rstrip("/")
    path = urllib.parse.urlsplit(url).path
    rest = path[len(base):] if path.startswith(base) else path
    first = rest.strip("/").split("/", 1)[0]
    return first if _RESOURCE.match(first) else "other"


def server_label(base_url: str) -> str:
    return (urllib.parse.urlsplit(base_url).hostname or "unknown").lower()


def outcome_of(status: int) -> str:
    """'ok' for 2xx, else the status class with the common ones named: '401', '429', '5xx' ..."""
    if 200 <= status < 300:
        return "ok"
    if status in (400, 401, 403, 404, 429):
        return str(status)
    return f"{status // 100}xx"


def _key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, _LABEL_VALUE.sub("_", str(v))[:80]) for k, v in labels.items()))


class Metrics:
    """A thread-safe registry. One per process (`METRICS`); tests make their own."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self._gauges: dict[str, dict[tuple, float]] = defaultdict(dict)
        self._hist: dict[str, dict[tuple, list[float]]] = defaultdict(dict)   # bucket counts + [sum, count]

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[name][_key(labels)] += amount

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[name][_key(labels)] = value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        with self._lock:
            row = self._hist[name].setdefault(_key(labels), [0.0] * (len(BUCKETS) + 2))
            for i, edge in enumerate(BUCKETS):
                if seconds <= edge:
                    row[i] += 1
            row[-2] += seconds
            row[-1] += 1

    def value(self, name: str, **labels: str) -> float:
        """A counter's or gauge's current value (0 when never set); for tests and the check script."""
        with self._lock:
            k = _key(labels)
            if name in self._gauges and k in self._gauges[name]:
                return self._gauges[name][k]
            return self._counters.get(name, {}).get(k, 0.0)

    def count(self, name: str, **labels: str) -> int:
        """How many observations a histogram has for these labels."""
        with self._lock:
            row = self._hist.get(name, {}).get(_key(labels))
            return int(row[-1]) if row else 0

    def total(self, name: str, **match: str) -> float:
        """A counter summed over every series whose labels include `match`."""
        want = set(_key(match))
        with self._lock:
            return sum(v for k, v in self._counters.get(name, {}).items() if want <= set(k))

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()

    def prometheus_text(self) -> str:
        """The registry in Prometheus text exposition format (version 0.0.4)."""
        out: list[str] = []
        with self._lock:
            names = sorted(set(self._counters) | set(self._gauges) | set(self._hist))
            for name in names:
                kind, text = HELP.get(name, ("untyped", name))
                out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
                for k, v in sorted(self._counters.get(name, {}).items()):
                    out.append(f"{name}{_fmt(k)} {_num(v)}")
                for k, v in sorted(self._gauges.get(name, {}).items()):
                    out.append(f"{name}{_fmt(k)} {_num(v)}")
                for k, row in sorted(self._hist.get(name, {}).items()):
                    for edge, n in zip(BUCKETS, row):
                        out.append(f"{name}_bucket{_fmt(k + (('le', _num(edge)),))} {_num(n)}")
                    out.append(f"{name}_bucket{_fmt(k + (('le', '+Inf'),))} {_num(row[-1])}")
                    out.append(f"{name}_sum{_fmt(k)} {row[-2]:.6f}")
                    out.append(f"{name}_count{_fmt(k)} {_num(row[-1])}")
        return "\n".join(out) + "\n"


def _fmt(key: tuple) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in key) + "}" if key else ""


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


METRICS = Metrics()
