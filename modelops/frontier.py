"""The frontier model that writes and labels training data, with a hard spending limit (ADR 037).

Every call goes through `Frontier`:
- **Budget.** The cost of each call is computed from the model's price in loop.yaml and added to
  `spend.json` in the run folder, before the next call is allowed. Once `max_usd` is reached,
  `BudgetExceeded` stops the loop; a resumed run continues the same account.
- **Cache.** A reply is stored under the SHA-256 of the model, settings and prompt. Re-running a round
  (after a crash, or to reproduce it) costs nothing and gives the same data.
- **JSON.** Replies are parsed leniently (fences, text around the object); a reply that is not JSON is
  asked for once more, then the batch is skipped and counted, never guessed.

Nothing here decides a label or keeps a message: modelops/guard_loop.py and answer_loop.py do, with
an independent second look and the cleaning rules.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm import providers, usage

Complete = Callable[[dict, list[dict]], tuple[str, dict]]


class BudgetExceeded(RuntimeError):
    """The run has spent its frontier budget."""


class FrontierError(RuntimeError):
    """The frontier model refused a request for a reason waiting will not fix (key, model, request)."""


class Frontier:
    RETRIES = 4

    def __init__(self, model: dict, max_usd: float, run_dir: Path, complete: Complete = providers.complete,
                 sleep: Callable[[float], None] = time.sleep):
        self.model, self.max_usd, self._complete, self._sleep = model, max_usd, complete, sleep
        self.cache = run_dir / "frontier_cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._spend_file = run_dir / "spend.json"
        self._lock = threading.Lock()
        self.calls = self.cached = self.unparsed = self.declined = 0
        self.declined_why: Counter = Counter()
        self._no_temperature = model.get("temperature", 0) is None
        self.parallel = int(model.get("parallel", 4))

    # --- money -------------------------------------------------------------------------------------

    @property
    def spent(self) -> float:
        try:
            return float(json.loads(self._spend_file.read_text())["usd"])
        except (FileNotFoundError, KeyError, ValueError):
            return 0.0

    def _charge(self, tokens: dict, messages: list[dict], text: str) -> None:
        tin = tokens.get("input") if tokens.get("input") is not None else usage.estimate_tokens(
            " ".join(m["content"] for m in messages))
        tout = tokens.get("output") if tokens.get("output") is not None else usage.estimate_tokens(text)
        cost = usage.price_of(self.model, tin, tout) or 0.0
        with self._lock:
            total = self.spent + cost
            self._spend_file.write_text(json.dumps({"usd": round(total, 6), "model": self.model["name"]}))

    def _check(self) -> None:
        if self.spent >= self.max_usd:
            raise BudgetExceeded(f"frontier budget spent: ${self.spent:.2f} of ${self.max_usd:.2f}")

    # --- calls -------------------------------------------------------------------------------------

    def text(self, messages: list[dict], temperature: float | None = None, salt: str = "") -> str:
        """The model's reply, from the cache when this exact request (and salt) was made before. The salt
        (round and batch) lets the same prompt be asked again for new writing in a later round."""
        model = dict(self.model)
        if temperature is not None and not self._no_temperature:
            model["temperature"] = temperature
        if self._no_temperature:
            model["temperature"] = None
        key = hashlib.sha256(json.dumps([model["name"], model.get("temperature"), model.get("max_tokens"),
                                         messages, salt], sort_keys=True).encode()).hexdigest()
        hit = self.cache / f"{key}.json"
        if hit.exists():
            self.cached += 1
            return json.loads(hit.read_text())["text"]
        self._check()
        text, tokens = self._call(model, messages)
        self.calls += 1
        self._charge(tokens or {}, messages, text)
        hit.write_text(json.dumps({"text": text}))
        return text

    def _call(self, model: dict, messages: list[dict]) -> tuple[str, dict]:
        """One request; a busy or overloaded API (429, 5xx, 529) is waited for and asked again, up to
        RETRIES times. Any other refusal (a wrong key, an unknown model) stops at once."""
        for attempt in range(self.RETRIES + 1):
            try:
                return self._complete(model, messages)
            except providers.ModelUnavailable as e:
                detail = getattr(e, "detail", "")
                if "no answer text" in str(e):
                    # The model declined this one request (or answered with no text). Skip it, count it, and
                    # keep the round going; the empty reply is cached so a rerun does not ask again.
                    self.declined += 1
                    self.declined_why[detail or "no text"] += 1
                    return "", {"input": None, "output": 0}
                if "temperature" in detail.lower() and model.get("temperature") is not None:
                    # this model refuses a temperature: leave it to the provider for the rest of the run
                    self._no_temperature = True
                    model = {**model, "temperature": None}
                    continue
                busy = re.search(r"HTTP (429|5\d\d)\b", str(e)) or "could not reach" in str(e)
                if not busy or attempt == self.RETRIES:
                    raise FrontierError(f"the frontier model refused the request: {e}"
                                        + (f": {detail}" if detail else "")) from None
                self._sleep(min(60.0, 4.0 * 2 ** attempt))
        raise AssertionError("unreachable")

    def many(self, requests: list[tuple[list[dict], float | None, str]], what: str = "requests") -> list[str]:
        """Replies to many requests, `parallel` at a time, in the order asked, with a progress line. Each is
        (messages, temperature, salt). The budget is checked before each call, so a run can go over by at
        most `parallel` calls."""
        from concurrent.futures import ThreadPoolExecutor

        done, total, out = 0, len(requests), [""] * len(requests)
        step = max(1, total // 10)
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            futures = {pool.submit(self.text, m, t, s): i for i, (m, t, s) in enumerate(requests)}
            try:
                for f in _completed(futures):
                    out[futures[f]] = f.result()
                    done += 1
                    if done % step == 0 or done == total:
                        skipped = f", {self.declined} declined" if self.declined else ""
                        print(f"    {what}: {done}/{total}  (${self.spent:.2f} spent, {self.cached} from cache{skipped})",
                              flush=True)
            except BaseException:
                for f in futures:
                    f.cancel()
                raise
        return out

    def json(self, messages: list[dict], temperature: float | None = None, salt: str = "") -> dict[str, Any] | None:
        """The JSON object in the reply; asked once more if there is none; None after that."""
        for attempt in range(2):
            asked = messages if attempt == 0 else [*messages, {"role": "user", "content":
                                                   "Reply with the JSON object only, nothing before or after it."}]
            obj = parse_json(self.text(asked, temperature, salt))
            if obj is not None:
                return obj
        self.unparsed += 1
        return None


def parse_json(reply: str) -> dict[str, Any] | None:
    """A JSON object from a reply that may wrap it in a code fence or a sentence."""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    for candidate in (s, (re.search(r"\{.*\}", s, re.S) or [None])[0]):
        if candidate:
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _completed(futures):
    from concurrent.futures import as_completed
    return as_completed(futures)
