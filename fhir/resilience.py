"""Resilience primitives for calls to a FHIR server (ADR 036).

Three small, independent pieces, each thread-safe and each with an injectable clock so tests never
sleep:

- `Deadline` and `budget()`: one time budget for everything a tool call does (sign-in, searches,
  pages, retries), carried in a context variable so the client sees it without every function
  passing it along. A budget nested inside another can only shorten it.
- `CircuitBreaker`: after a run of failures, stop calling a server that is down for a while and say
  so at once, instead of making every question wait for its own timeout. One trial call is let
  through after the pause; its outcome closes or reopens the breaker.
- `TokenBucket`: a client-side rate limit, so a burst of questions (or the chart brief's parallel
  reads) stays under what the EHR allows instead of being answered with 429s.

Nothing here knows about HTTP or FHIR; `fhir/transport.py` puts them together.
"""

from __future__ import annotations

import contextvars
import enum
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

log = logging.getLogger(__name__)

Clock = Callable[[], float]


# --- the time budget --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Deadline:
    """A point in time (on `clock`) after which no new request should start."""

    expires_at: float
    clock: Clock = time.monotonic

    def remaining(self) -> float:
        """Seconds left; never negative."""
        return max(0.0, self.expires_at - self.clock())

    @property
    def expired(self) -> bool:
        return self.clock() >= self.expires_at


_DEADLINE: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("fhir_deadline", default=None)


def current_deadline() -> Deadline | None:
    """The deadline of the tool call in progress, or None when no budget was set."""
    return _DEADLINE.get()


@contextmanager
def budget(seconds: float, clock: Clock = time.monotonic) -> Iterator[Deadline]:
    """Run the block within `seconds`. Nested budgets keep the earlier deadline.

    Threads started inside the block do not inherit context variables on their own: submit work
    with `contextvars.copy_context().run` (see `run_in_context`).
    """
    if seconds <= 0:
        raise ValueError("a budget must be a positive number of seconds")
    proposed = Deadline(clock() + seconds, clock)
    outer = _DEADLINE.get()
    chosen = outer if outer is not None and outer.expires_at <= proposed.expires_at else proposed
    token = _DEADLINE.set(chosen)
    try:
        yield chosen
    finally:
        _DEADLINE.reset(token)


def bounded_timeout(timeout: float) -> float:
    """`timeout`, shortened to what is left of the current budget (if any)."""
    d = _DEADLINE.get()
    return timeout if d is None else min(timeout, d.remaining())


def run_in_context(fn: Callable, *args, **kwargs):
    """A callable for a thread pool that runs `fn` with a copy of the caller's context (its budget
    and cache scope). Each task gets its own copy: one Context cannot be entered by two threads."""
    ctx = contextvars.copy_context()

    def call():
        return ctx.run(fn, *args, **kwargs)
    return call


# --- the circuit breaker ----------------------------------------------------------------------------

class BreakerState(enum.Enum):
    CLOSED = "closed"          # calls go through
    OPEN = "open"              # calls fail at once until the pause ends
    HALF_OPEN = "half_open"    # one trial call is in flight; the others fail at once


class CircuitBreaker:
    """Counts consecutive failures of one server; opens after `failures` of them, for `reset_seconds`.

    `allow()` before a call, then `record(success)` after it, or `abandon()` when the call was never
    made. A caller that got `allow() == True` must do one of the two, or a half-open breaker stays
    half-open.
    """

    def __init__(self, name: str, failures: int = 5, reset_seconds: float = 30.0,
                 clock: Clock = time.monotonic,
                 on_change: Callable[[str, BreakerState], None] | None = None):
        if failures < 1 or reset_seconds <= 0:
            raise ValueError("failures must be at least 1 and reset_seconds positive")
        self.name, self.failures, self.reset_seconds = name, failures, reset_seconds
        self._clock, self._on_change = clock, on_change
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._count = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    def retry_in(self) -> float:
        """Seconds until a trial call is let through (0 when not open)."""
        with self._lock:
            if self._state is not BreakerState.OPEN:
                return 0.0
            return max(0.0, self._opened_at + self.reset_seconds - self._clock())

    def allow(self) -> bool:
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN and self._clock() >= self._opened_at + self.reset_seconds:
                self._set(BreakerState.HALF_OPEN)
                return True                          # this caller is the trial
            return False                             # open, or a trial is already in flight

    def record(self, success: bool) -> None:
        with self._lock:
            if success:
                self._count = 0
                if self._state is not BreakerState.CLOSED:
                    self._set(BreakerState.CLOSED)
                return
            self._count += 1
            if self._state is BreakerState.HALF_OPEN or self._count >= self.failures:
                self._opened_at = self._clock()
                if self._state is not BreakerState.OPEN:
                    self._set(BreakerState.OPEN)

    def abandon(self) -> None:
        """The caller got `allow()` but never sent (its budget or the queue stopped it). A trial
        call that never happened decides nothing: the next caller may try at once."""
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._opened_at = self._clock() - self.reset_seconds
                self._set(BreakerState.OPEN)

    def _set(self, state: BreakerState) -> None:      # called with the lock held
        if state is self._state:
            return
        self._state = state
        log.warning("circuit breaker %s: %s", self.name, state.value)
        if self._on_change is not None:
            self._on_change(self.name, state)


# --- the rate limit ---------------------------------------------------------------------------------

class RateLimited(Exception):
    """No token could be had within the time allowed."""


class TokenBucket:
    """`rate` requests per second on average, with bursts of up to `burst`."""

    def __init__(self, rate: float, burst: int, clock: Clock = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self.rate, self.burst = float(rate), float(burst)
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._tokens = self.burst
        self._stamp = clock()

    def _refill(self) -> None:                       # called with the lock held
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def try_acquire(self) -> float:
        """Take a token if one is there (returns 0), else the seconds until one will be."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self.rate

    def acquire(self, max_wait: float) -> float:
        """Wait for a token for at most `max_wait` seconds; the seconds waited. Raises RateLimited
        when the wait would be longer (the caller fails fast instead of overrunning its budget)."""
        waited = 0.0
        while True:
            wait = self.try_acquire()
            if wait == 0.0:
                return waited
            if waited + wait > max_wait:
                raise RateLimited(f"rate limit: next request allowed in {wait:.1f} s")
            self._sleep(wait)
            waited += wait
