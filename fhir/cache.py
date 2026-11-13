"""A short-lived cache of FHIR answers, one compartment per signed-in person (ADR 036).

Within a conversation the assistant often reads the same records twice in a minute (the records
search, then the chart brief). With this cache the second read is served from memory.

Rules, each enforced here rather than left to the caller:

- **Only for a person's own sign-in.** An entry is stored under a scope that names the session
  (`launch:<session ref>`, `user:<token file>`). Without a scope nothing is cached: the backend
  app's identity is shared by every user, and the hospital's audit log should record each person's
  access when it happens.
- **Short.** At most 120 seconds (the mapping's `cache_seconds`, validated in profiles/fhir_map.py).
- **Bounded.** Least recently used entries go first, past `max_entries`.
- **Copies out.** A caller that edits what it got cannot change what the next caller gets.
- **Answers only.** Only a validated 200 answer is stored; errors are never cached.

The scope comes from the client's sign-in (`cache_scope` on the auth object). The cache itself
lives on the server's shared transport, so the client built for each launched call reuses it.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fhir.metrics import METRICS, Metrics

MAX_SECONDS = 120


class ResponseCache:
    def __init__(self, ttl: float, max_entries: int = 512, clock: Callable[[], float] = time.monotonic,
                 metrics: Metrics = METRICS):
        if not 0 < ttl <= MAX_SECONDS:
            raise ValueError(f"cache ttl must be between 0 and {MAX_SECONDS} seconds")
        self.ttl, self.max_entries, self._clock, self._metrics = ttl, max_entries, clock, metrics
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()

    @staticmethod
    def key(scope: str, url: str, params: dict | None) -> tuple:
        return scope, url, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))

    def get(self, scope: str | None, url: str, params: dict | None) -> dict | None:
        if not scope:
            return None
        k = self.key(scope, url, params)
        with self._lock:
            item = self._items.get(k)
            if item is None or self._clock() >= item[0]:
                self._items.pop(k, None)
                self._metrics.inc("fhir_cache_total", result="miss")
                return None
            self._items.move_to_end(k)
            self._metrics.inc("fhir_cache_total", result="hit")
            return copy.deepcopy(item[1])

    def put(self, scope: str | None, url: str, params: dict | None, body: dict) -> None:
        if not scope:
            return
        with self._lock:
            self._items[self.key(scope, url, params)] = (self._clock() + self.ttl, copy.deepcopy(body))
            self._items.move_to_end(self.key(scope, url, params))
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def drop_scope(self, scope: str) -> None:
        """Forget everything for one session (it ended, or its token was refused)."""
        with self._lock:
            for k in [k for k in self._items if k[0] == scope]:
                del self._items[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
