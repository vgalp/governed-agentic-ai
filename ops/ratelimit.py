"""How many chat messages one person may send (audit SC-10, ADR 030).

A token bucket per person: `burst` messages at once, then `per_minute` a minute. With sign-in on
(ADR 028) the person is the signed-in user, so one person cannot use up the local model for
everyone else; in demo mode, where anyone can claim any name, it is the caller's network address.
Bounded memory: the least recently seen people are forgotten first.

Profile setting (profile.yaml), optional:
    rate_limit: {per_minute: 20, burst: 10}
"""

import threading
import time
from collections import OrderedDict

DEFAULT_PER_MINUTE = 20
DEFAULT_BURST = 10
MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self, per_minute: int = DEFAULT_PER_MINUTE, burst: int = DEFAULT_BURST, clock=time.monotonic,
                 max_keys: int = MAX_KEYS):
        if per_minute < 1 or burst < 1:
            raise ValueError("rate_limit needs per_minute and burst of at least 1")
        self.rate = per_minute / 60.0
        self.burst, self.clock, self.max_keys = float(burst), clock, max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def for_profile(cls, profile) -> "RateLimiter":
        s = getattr(profile, "rate_limit", None) or {}
        return cls(int(s.get("per_minute", DEFAULT_PER_MINUTE)), int(s.get("burst", DEFAULT_BURST)))

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds to wait before the next message would be allowed)."""
        now = self.clock()
        with self._lock:
            tokens, at = self._buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                allowed, wait = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, wait = False, (1 - tokens) / self.rate
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        return allowed, wait
