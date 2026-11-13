"""How requests reach one FHIR server: shared, bounded and measured (ADR 036).

A `Transport` is shared by every client of the same server in a process (the backend client, and
the client built for each call launched from the chart). It owns:

- a pooled HTTP session: connections are reused instead of a new TLS handshake per request;
- a concurrency limit: at most `max_concurrent` requests in flight to the server;
- an optional rate limit (`rate_limit` in fhir.yaml), so bursts stay under the EHR's limits;
- a circuit breaker: after `breaker.failures` consecutive failures the server is not called for
  `breaker.reset_seconds`, and the assistant says so at once instead of waiting for timeouts;
- the per-session response cache (fhir/cache.py), when `cache_seconds` is set;
- metrics for every attempt (fhir/metrics.py).

`send()` makes exactly one HTTP attempt. Retrying, signing in again and reading the answer are the
client's job (fhir/client.py), which calls `send()` once per attempt. Every wait (rate limit,
queue, timeout) is cut to what is left of the call's budget (fhir/resilience.budget).
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from fhir.cache import ResponseCache
from fhir.errors import FhirDeadlineError, FhirUnavailableError
from fhir.metrics import METRICS, Metrics, outcome_of, server_label
from fhir.resilience import (
    BreakerState,
    CircuitBreaker,
    RateLimited,
    TokenBucket,
    bounded_timeout,
    budget,
    current_deadline,
)

SERVER_FAILURES = frozenset({500, 502, 503, 504})   # count against the breaker; 4xx and 429 do not


class FhirTimeoutError(FhirUnavailableError):
    """One attempt timed out (the client may try again)."""


class FhirUnreachableError(FhirUnavailableError):
    """One attempt could not connect or lost the connection (the client may try again)."""


@dataclass(frozen=True)
class ServerPolicy:
    """How one server is called. Built from the mapping's `server:` block (`from_settings`), whose
    values profiles/fhir_map.py has already checked."""

    timeout: float = 20.0                  # seconds, one attempt
    retries: int = 2                       # further attempts after a busy answer, a timeout or a lost connection
    max_concurrent: int = 4                # requests in flight to the server, per process
    pool_size: int = 8                     # kept-alive connections
    rate_per_second: float | None = None   # client-side rate limit; None: none
    rate_burst: int = 1
    breaker_failures: int = 5
    breaker_reset_seconds: float = 30.0
    budget_seconds: float = 75.0           # one tool call, all requests together; below the tool's own timeout
    cache_seconds: float = 0.0             # per-session cache; 0: off

    @classmethod
    def from_settings(cls, server: dict[str, Any]) -> ServerPolicy:
        rate = server.get("rate_limit") or {}
        breaker = server.get("breaker") or {}
        d = cls()
        return cls(
            timeout=float(server.get("timeout", d.timeout)),
            retries=int(server.get("retries", d.retries)),
            max_concurrent=int(server.get("max_concurrent", d.max_concurrent)),
            pool_size=int(server.get("pool_size", d.pool_size)),
            rate_per_second=float(rate["per_second"]) if rate.get("per_second") else None,
            rate_burst=int(rate.get("burst", max(1, int(rate.get("per_second", 1))))),
            breaker_failures=int(breaker.get("failures", d.breaker_failures)),
            breaker_reset_seconds=float(breaker.get("reset_seconds", d.breaker_reset_seconds)),
            budget_seconds=float(server.get("budget", d.budget_seconds)),
            cache_seconds=float(server.get("cache_seconds", d.cache_seconds)),
        )


def pooled_session(pool_size: int) -> requests.Session:
    """A session that keeps up to `pool_size` connections to a host alive. Retries are off at the
    urllib3 level: the client decides what may be tried again."""
    s = requests.Session()
    # pool_connections: hosts kept (the FHIR server, its token server); pool_maxsize: connections per host
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=pool_size, max_retries=0, pool_block=False)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class Transport:
    """The process's way of calling one FHIR server (see the module docstring). Thread-safe."""

    def __init__(self, base_url: str, policy: ServerPolicy, session=None, metrics: Metrics = METRICS,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.policy, self.metrics, self._clock = policy, metrics, clock
        self.server = server_label(base_url)
        self.session = session if session is not None else pooled_session(policy.pool_size)
        self.breaker = CircuitBreaker(self.server, policy.breaker_failures, policy.breaker_reset_seconds,
                                      clock=clock, on_change=self._breaker_changed)
        self.limiter = (TokenBucket(policy.rate_per_second, policy.rate_burst, clock=clock, sleep=sleep)
                        if policy.rate_per_second else None)
        self.cache = ResponseCache(policy.cache_seconds, clock=clock, metrics=metrics) if policy.cache_seconds else None
        self._slots = threading.BoundedSemaphore(policy.max_concurrent)

    def send(self, url: str, *, params: dict | None, headers: dict[str, str], resource: str) -> requests.Response:
        """One GET. Returns the response whatever its status; raises FhirDeadlineError when the budget
        is spent, FhirUnavailableError when the breaker is open, FhirTimeoutError or
        FhirUnreachableError when the attempt itself failed."""
        self._check_budget()
        if not self.breaker.allow():
            self._reject("breaker")
            raise FhirUnavailableError(f"{self.server} is not answering; the assistant will try it again in "
                                       f"{self.breaker.retry_in():.0f} s")
        sent = False
        try:
            self._admit()
            try:
                timeout = bounded_timeout(self.policy.timeout)
                if timeout <= 0:
                    self._reject("deadline")
                    raise FhirDeadlineError("the time for this question ran out before the records were read")
                sent = True
                return self._attempt(url, params, headers, resource, timeout)
            finally:
                self._slots.release()
        finally:
            if not sent:
                self.breaker.abandon()         # a trial call that never went out decides nothing

    # --- the steps ------------------------------------------------------------------------------------

    def _check_budget(self) -> None:
        d = current_deadline()
        if d is not None and d.expired:
            self._reject("deadline")
            raise FhirDeadlineError("the time for this question ran out before the records were read")

    def _admit(self) -> None:
        """A rate-limit token, then a free slot; neither waits past the budget or one timeout."""
        wait = bounded_timeout(self.policy.timeout)
        if self.limiter is not None:
            try:
                wait -= self.limiter.acquire(wait)
            except RateLimited:
                self._reject("rate")
                raise FhirDeadlineError(f"too many requests to {self.server} right now; "
                                        "the rate limit in fhir.yaml would be exceeded") from None
        if not self._slots.acquire(timeout=max(0.0, wait)):
            self._reject("queue")
            raise FhirDeadlineError(f"too many requests to {self.server} are already waiting")

    def _attempt(self, url: str, params: dict | None, headers: dict[str, str], resource: str,
                 timeout: float) -> requests.Response:
        labels = {"server": self.server, "resource": resource}
        start = self._clock()
        try:
            r = self.session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.Timeout:
            self.metrics.inc("fhir_requests_total", outcome="timeout", **labels)
            self.breaker.record(False)
            raise FhirTimeoutError(f"{self.server} did not answer within {timeout:g} s") from None
        except requests.RequestException as e:
            self.metrics.inc("fhir_requests_total", outcome="unreachable", **labels)
            self.breaker.record(False)
            raise FhirUnreachableError(f"could not reach {self.server} ({type(e).__name__})") from None
        except BaseException:
            self.breaker.abandon()             # not the server's doing (a bug, an interrupt): decides nothing
            raise
        self.metrics.observe("fhir_request_seconds", self._clock() - start, **labels)
        self.metrics.inc("fhir_requests_total", outcome=outcome_of(r.status_code), **labels)
        self.breaker.record(r.status_code not in SERVER_FAILURES)
        return r

    def _reject(self, reason: str) -> None:
        self.metrics.inc("fhir_rejected_total", server=self.server, reason=reason)

    def _breaker_changed(self, name: str, state: BreakerState) -> None:
        self.metrics.set("fhir_breaker_open", 0.0 if state is BreakerState.CLOSED else 1.0, server=name)


# --- one transport per server, per process ----------------------------------------------------------

_SHARED: dict[tuple, Transport] = {}
_SHARED_LOCK = threading.Lock()


def _origin(base_url: str) -> tuple[str, str, int | None]:
    u = urllib.parse.urlsplit(base_url)
    return u.scheme, (u.hostname or "").lower(), u.port


def shared_transport(base_url: str, policy: ServerPolicy) -> Transport:
    """The process's transport for this server and policy (created on first use)."""
    key = (_origin(base_url), policy)
    with _SHARED_LOCK:
        t = _SHARED.get(key)
        if t is None:
            t = _SHARED[key] = Transport(base_url, policy)
        return t


def reset_shared_transports() -> None:
    """Forget every shared transport (tests; a process that reloads its profile)."""
    with _SHARED_LOCK:
        _SHARED.clear()


def request_budget(server: dict[str, Any] | None) -> AbstractContextManager:
    """The time budget for one tool call on this server: `with request_budget(map.server): ...`
    No server (a profile without fhir.yaml): no budget."""
    if server is None:
        return nullcontext()
    return budget(ServerPolicy.from_settings(server).budget_seconds)


__all__ = ["FhirTimeoutError", "FhirUnreachableError", "ServerPolicy", "Transport",
           "pooled_session", "request_budget", "reset_shared_transports", "shared_transport"]
