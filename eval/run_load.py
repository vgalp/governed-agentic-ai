"""Load and stress tests for one machine: where the time goes, and where it stops coping.

    uv run python -m eval.run_load components                 # each part alone, in this process
    uv run python -m eval.run_load gateway                    # the governed gateway's own path, many callers
    uv run python -m eval.run_load chat                       # the whole pipeline, question to answer

    components   masking, the guard, the sensitive-category lists and (if the profile names one) the trained
                 classifier, and a policy decision. No network but the local policy engine. Says which part
                 costs what, before anything is blamed on the language it is written in.
    gateway      POST /check on the gateway (sign-in check, rate limit, guard, masking, audit) at rising
                 numbers of callers. It never calls a record tool, so it never loads a vendor's sandbox:
                 load-testing somebody else's server is not ours to do, and it would measure their server.
    chat         POST /chat on the API and wait for the answer, at rising numbers of people. Run it on a
                 profile whose records are local (clinic-assistant), never on a vendor sandbox profile.

Each level reports requests, failures by kind, requests a second, and the median, 95th and 99th percentile
and slowest response. A level's results are worth reading only beside its failures: a fast level that
refused half its requests is not fast.

What a single laptop can and cannot show: the order of the costs and the first thing to give way, yes.
The numbers a hospital would see, no: the answering model, the record system and the hardware all differ.

Limits to lift for the test (and put back): the gateway allows each caller 120 calls a minute (outside.yaml
rate_limit) and the API each person 20 messages a minute (profile.yaml rate_limit). Refusals for that
reason are counted on their own line, so a limit doing its job is not read as a failure.

Output, in eval/results/ (commit it): load_<mode>_<time>.json and a .md table.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
GATEWAY, API = "http://127.0.0.1:8110", "http://127.0.0.1:8000"
OK, LIMITED = "ok", "rate limited"

# Invented questions: ordinary ones, one the checks stop, one with identifiers to mask.
QUESTIONS = (
    "What is the clinic's cancellation policy for a missed appointment?",
    "How do I book an interpreter for a visit next week?",
    "Which forms does a new patient need to fill in before the first visit?",
    "What dose of metformin should this patient take?",
    "Call Halima Yusuf back on 555-201-7788 about the refill; her record number is 00482913.",
    "What are the opening hours of the lab on Saturdays?",
    "A patient asks whether she can bring her service dog to the appointment. What do I tell her?",
    "Where do I send a referral for physiotherapy?",
)


def percentile(values: list[float], share: float) -> float:
    """The value below which `share` of the sorted values fall (nearest rank); 0 for none."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * len(ordered)) - 1))]


def summarize(outcomes: list[tuple[str, float]], seconds: float) -> dict:
    """One level's numbers from (outcome, seconds taken) pairs. Timings are of the requests that worked."""
    kinds = Counter(kind for kind, _ in outcomes)
    took = [t for kind, t in outcomes if kind == OK]
    return {"requests": len(outcomes), "ok": kinds[OK], "rate_limited": kinds[LIMITED],
            "failed": {k: n for k, n in sorted(kinds.items()) if k not in (OK, LIMITED)},
            "per_second": round(kinds[OK] / seconds, 2) if seconds > 0 else 0.0,
            "median_s": round(statistics.median(took), 5) if took else 0.0,
            "p95_s": round(percentile(took, 0.95), 5), "p99_s": round(percentile(took, 0.99), 5),
            "slowest_s": round(max(took), 5) if took else 0.0}


def run_level(call: Callable[[int], str], workers: int, seconds: float | None = None, requests_each: int | None = None,
              clock: Callable[[], float] = time.monotonic) -> dict:
    """`workers` threads call `call(worker number)` (which returns an outcome: "ok", "rate limited" or what
    went wrong) for `seconds`, or `requests_each` times each. An exception is an outcome, not a crash."""
    if (seconds is None) == (requests_each is None):
        raise ValueError("give seconds or requests_each, not both")
    outcomes: list[tuple[str, float]] = []
    lock = threading.Lock()
    started = clock()

    def work(n: int) -> None:
        done = 0
        while (clock() - started < seconds) if seconds is not None else (done < requests_each):
            t0 = clock()
            try:
                kind = call(n)
            except Exception as e:  # noqa: BLE001 - a load test records every way a request can fail
                kind = type(e).__name__
            with lock:
                outcomes.append((kind, clock() - t0))
            done += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))
    return {"callers": workers, **summarize(outcomes, clock() - started)}


def shown(seconds: float) -> str:
    """A time as a person reads it: milliseconds under a second, seconds above."""
    return f"{seconds * 1000:.1f} ms" if seconds < 1 else f"{seconds:.2f} s"


def table(levels: list[dict], first: str = "Callers at once") -> str:
    lines = [f"| {first} | Requests | Worked | Refused by the limit | Failed | A second | Median | 95th | 99th | Slowest |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for lv in levels:
        failed = ", ".join(f"{n} {k}" for k, n in lv["failed"].items()) or "0"
        lines.append(f"| {lv.get('name', lv.get('callers'))} | {lv['requests']} | {lv['ok']} | {lv['rate_limited']} | {failed} | "
                     f"{lv['per_second']} | {shown(lv['median_s'])} | {shown(lv['p95_s'])} | {shown(lv['p99_s'])} | "
                     f"{shown(lv['slowest_s'])} |")
    return "\n".join(lines)


def first_strain(levels: list[dict], slower_by: float = 3.0) -> str:
    """A sentence on where it stopped coping: the first level with failures, or whose median is `slower_by`
    times the first level's. Plain reading help; the table is the result."""
    base = next((lv["median_s"] for lv in levels if lv["ok"]), 0.0)
    for lv in levels:
        if lv["failed"]:
            return f"First failures at {lv['callers']} at once ({sum(lv['failed'].values())} of {lv['requests']} requests)."
        if base and lv["median_s"] >= slower_by * base:
            return (f"No failures, but at {lv['callers']} at once the median response was {shown(lv['median_s'])}, "
                    f"{lv['median_s'] / base:.1f} times the unloaded {shown(base)}.")
    return "No failures and no level more than three times slower than the first: the limit is above what was tried."


# ---- components

def component_parts(profile, classifier: str | None) -> list[tuple[str, Callable[[], Callable[[int], object]]]]:
    """(name, a function that builds the part) for each part. Built one at a time, so a part that cannot be
    loaded on this machine is reported as not available and the rest are still timed."""
    texts = QUESTIONS

    def scrub():
        from privacy.tokens import scrub_for_audit
        return lambda i: scrub_for_audit(texts[i % len(texts)])

    def masking():
        from privacy.masking import mask
        return lambda i: mask(texts[i % len(texts)], profile)

    def guard():
        from guardrails.check import check_text
        return lambda i: check_text(texts[i % len(texts)], profile)

    def policy():
        from gateway import gateway
        return lambda i: gateway.allowed_sensitive(profile, "clinician")

    def lists():
        from privacy import sensitivity
        detector = sensitivity.Detector(sensitivity.load(profile.sensitivity.value_sets or sensitivity.DEFAULT))
        return lambda i: detector.of_text(texts[i % len(texts)])

    def reader():
        from privacy import sensitivity
        from privacy.sensitivity_model import classifier_reader
        return classifier_reader(classifier or profile.sensitivity.text_model,
                                 sensitivity.load(profile.sensitivity.value_sets or sensitivity.DEFAULT))

    def one_text():
        read = reader()
        return lambda i: read(texts[i % len(texts)])

    def chart():
        read, records = reader(), [texts[k % len(texts)] for k in range(60)]
        return lambda i: read.many(records)

    parts = [("Audit scrub (dates, numbers)", scrub), ("Masking (names, contacts)", masking),
             ("Guard (rules and classifier)", guard), ("Policy decision (OPA)", policy)]
    if profile.sensitivity:
        parts.append(("Sensitive categories: lists, one text", lists))
        if classifier or profile.sensitivity.text_model:
            parts += [("Sensitive categories: classifier, one text", one_text),
                      ("Sensitive categories: classifier, a 60-record chart", chart)]
    return parts


def component_levels(parts, rounds: int) -> list[dict]:
    """Each part alone, one call after another, then with four callers at once."""
    out = []
    for label, build in parts:
        ticks = itertools.count()
        try:
            fn = build()
            fn(next(ticks))                                # first use loads models: not part of the timing
        except Exception as e:  # noqa: BLE001 - a missing package or a stopped service is a result, not a crash
            out.append({"name": label, **summarize([(f"not available ({type(e).__name__})", 0.0)], 1.0)})
            continue

        def call(n: int, fn=fn, ticks=ticks) -> str:
            fn(next(ticks))                                # a different text each call, whoever calls
            return OK
        for callers in (1, 4):
            out.append({"name": f"{label}, {callers} at once", **run_level(call, callers, requests_each=max(1, rounds // callers))})
    return out


# ---- the gateway's own path

def gateway_call(url: str, tokens: list[str], post) -> Callable[[int], str]:
    def call(n: int) -> str:
        r = post(f"{url}/check", json={"text": QUESTIONS[n % len(QUESTIONS)]},
                 headers={"Authorization": f"Bearer {tokens[n % len(tokens)]}"}, timeout=60)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        body = r.json()
        if body.get("error") == "rate_limited":
            return LIMITED
        return OK if "error" not in body else str(body["error"])     # a stopped question is the check working
    return call


# ---- the whole pipeline

def chat_call(url: str, role: str, post, get, wait_seconds: float, sleep=time.sleep, clock=time.monotonic) -> Callable[[int], str]:
    def call(n: int) -> str:
        r = post(f"{url}/chat", json={"user_id": f"load-{n}", "role": role, "message": QUESTIONS[n % len(QUESTIONS)]}, timeout=30)
        if r.status_code == 429:
            return LIMITED
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        request_id, deadline = r.json()["request_id"], clock() + wait_seconds
        while clock() < deadline:
            answer = get(f"{url}/chat/{request_id}", timeout=30).json()
            if answer.get("status") == "done":
                return OK
            sleep(0.25)
        return f"no answer in {wait_seconds:.0f} s"
    return call


def main() -> None:
    ap = argparse.ArgumentParser(description="Load and stress tests for one machine.")
    ap.add_argument("mode", choices=["components", "gateway", "chat"])
    ap.add_argument("--profile", default=None, help="components: the profile whose settings to use")
    ap.add_argument("--classifier", help="components: a trained classifier in models/ to time, if the profile names none")
    ap.add_argument("--rounds", type=int, default=200, help="components: calls of each part")
    ap.add_argument("--levels", default=None, help="callers at once, e.g. 1,2,4,8 (gateway: 1,2,4,8,16,32; chat: 1,2,4,8)")
    ap.add_argument("--seconds", type=float, default=20, help="gateway: how long each level runs")
    ap.add_argument("--requests-each", type=int, default=3, help="chat: questions each person asks at a level")
    ap.add_argument("--wait", type=float, default=180, help="chat: seconds to wait for one answer")
    ap.add_argument("--url", default=None)
    ap.add_argument("--role", default="clinician", help="chat: the role asking (demo sign-in)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    notes: list[str] = []
    if args.mode == "components":
        from profiles.loader import load_profile
        profile = load_profile(args.profile) if args.profile else load_profile()
        levels, first = component_levels(component_parts(profile, args.classifier), args.rounds), "Part"
        notes.append(f"Profile {profile.name}. Each part was called once before timing, so loading a model is not counted.")
    else:
        import requests
        counts = [int(x) for x in (args.levels or ("1,2,4,8,16,32" if args.mode == "gateway" else "1,2,4,8")).split(",")]
        first = "Callers at once" if args.mode == "gateway" else "People at once"
        if args.mode == "gateway":
            tokens = [t for t in (os.environ.get("GATEWAY_TOKENS") or os.environ.get("GATEWAY_TOKEN") or "").split(",") if t.strip()]
            if not tokens:                                 # none given: four saved development callers
                from ops.dev_identity import saved_token
                tokens = [saved_token("clinician", f"dev-load-{k}") for k in range(1, 5)]
            call = gateway_call(args.url or GATEWAY, tokens, requests.post)
            notes.append(f"{len(tokens)} caller token(s). POST /check only: no record tool and no record system is called.")
            levels = []
            for c in counts:
                print(f"  {c} at once for {args.seconds:.0f} s ...", flush=True)
                levels.append(run_level(call, c, seconds=args.seconds))
        else:
            call = chat_call(args.url or API, args.role, requests.post, requests.get, args.wait)
            notes.append(f"Role {args.role}. Each person asked {args.requests_each} question(s) and waited for each answer.")
            levels = []
            for c in counts:
                print(f"  {c} at once ...", flush=True)
                levels.append(run_level(call, c, requests_each=args.requests_each))
        notes.append(first_strain(levels))
        if any(lv["rate_limited"] for lv in levels):
            notes.append("Some requests were refused by the rate limit. That is the limit working; raise it for the test "
                         "(see this file's first lines) to measure past it.")
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    stamp = datetime.now().astimezone()
    md = (f"# Load test: {args.mode}\n\nOne machine ({os.cpu_count()} processors"
          + (f", load average {load:.1f} at the end" if load is not None else "") + f"). {stamp.isoformat(timespec='seconds')}.\n\n"
          + table(levels, first) + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n")
    print("\n" + md)
    if not args.no_save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        path = RESULTS / f"load_{args.mode}_{stamp.strftime('%Y%m%d-%H%M')}.json"
        path.write_text(json.dumps({"mode": args.mode, "run_at": stamp.isoformat(timespec="seconds"), "levels": levels,
                                    "notes": notes}, indent=1) + "\n")
        path.with_suffix(".md").write_text(md, encoding="utf-8")
        print(f"Saved {path.relative_to(RESULTS.parent.parent)} and the .md table. Commit both.")


if __name__ == "__main__":
    main()
