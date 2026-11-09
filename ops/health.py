"""Liveness (audit OC-3, ADR 030): each worker writes a small heartbeat file; /health reads them.

A worker in the middle of a long request says so ("working", with when it started), so a request
that takes minutes on a laptop is not reported as a dead worker. One machine only: the files are in
logs/health/ (or GOVERNED_HEALTH_DIR); a deployment across machines would report to its monitoring
system instead.
"""

import json
import os
import time
from pathlib import Path

HEALTH_DIR = Path(os.environ.get("GOVERNED_HEALTH_DIR") or Path(__file__).resolve().parent.parent / "logs" / "health")
BEAT_SECONDS = 5           # how often an idle worker writes
STALE_SECONDS = 30         # idle and silent longer than this: not alive
WORK_LIMIT = 900           # one request may take this long (the planner's max.poll.interval) before it counts as stuck


class Heartbeat:
    def __init__(self, name: str, directory: Path | None = None, clock=time.time):
        self.name, self.dir, self.clock = name, Path(directory or HEALTH_DIR), clock
        self._last = -1e18
        self.state, self.since = "starting", clock()

    def _write(self, **extra) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"name": self.name, "pid": os.getpid(), "at": self.clock(), "state": self.state,
                "since": self.since, **extra}
        tmp = self.dir / f".{self.name}.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(self.dir / f"{self.name}.json")        # atomic: a reader never sees half a file
        self._last = self.clock()

    def beat(self, **extra) -> None:
        """Idle and polling. Writes at most every BEAT_SECONDS."""
        if self.state != "idle":
            self.state, self.since = "idle", self.clock()
            self._write(**extra)
        elif self.clock() - self._last >= BEAT_SECONDS:
            self._write(**extra)

    def working(self, what: str = "") -> None:
        self.state, self.since = "working", self.clock()
        self._write(task=what[:80])

    def set(self, state: str, **extra) -> None:
        self.state, self.since = state, self.clock()
        self._write(**extra)


def read(name: str, directory: Path | None = None, clock=time.time) -> dict:
    """A worker's state: ok, stuck, silent, gave_up, or missing (never reported)."""
    f = Path(directory or HEALTH_DIR) / f"{name}.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return {"name": name, "status": "missing"}
    now = clock()
    age = now - float(d.get("at", 0))
    if d.get("state") == "gave_up":
        status = "gave_up"
    elif d.get("state") == "working":
        status = "stuck" if now - float(d.get("since", now)) > WORK_LIMIT else "ok"
    else:
        status = "silent" if age > STALE_SECONDS else "ok"
    return {"name": name, "status": status, "state": d.get("state"), "age_seconds": round(age, 1),
            **{k: d[k] for k in ("restarts", "last_exit", "task") if k in d}}


# --- threads inside one process (the API's Kafka readers) ---------------------------------------

_ticks: dict[str, float] = {}


def tick(name: str, clock=time.time) -> None:
    """Called on every pass of a reader loop; /health checks how long ago it was."""
    _ticks[name] = clock()


def tick_age(name: str, clock=time.time) -> float | None:
    at = _ticks.get(name)
    return None if at is None else clock() - at


def assess(broker: tuple[bool, str | None], readers: dict[str, tuple[bool, float | None]],
           workers: dict[str, dict], supervisors: dict[str, dict], delivery: dict) -> tuple[dict, int]:
    """The /health verdict. ok: everything answers. degraded: answering, but something needs
    attention. down: the API cannot do its job (no broker, or one of its readers died); HTTP 503."""
    problems, warnings = [], []
    ok_broker, err = broker
    if not ok_broker:
        problems.append(f"Kafka is not reachable ({err})")
    reader_report = {}
    for name, (thread_alive, age) in readers.items():
        alive = thread_alive and age is not None and age < STALE_SECONDS
        reader_report[name] = {"alive": alive, "age_seconds": None if age is None else round(age, 1)}
        if not alive:
            problems.append(f"the {name} reader has stopped")
    worker_report = {}
    for w, s in workers.items():
        if supervisors.get(w, {}).get("status") == "gave_up":
            s = {**s, "status": "gave_up", "restarts": supervisors[w].get("restarts")}
        worker_report[w] = s
        if s["status"] != "ok":
            warnings.append(f"{w}: {s['status']}")
    if delivery.get("audit_lost"):
        warnings.append(f"{delivery['audit_lost']} audit event(s) were not delivered: the audit chain has a gap")
    if delivery.get("recent_failures"):
        warnings.append(f"{delivery['recent_failures']} Kafka delivery failure(s) in the last 5 minutes")
    status = "down" if problems else ("degraded" if warnings else "ok")
    return ({"status": status, "problems": problems, "warnings": warnings, "broker": ok_broker,
             "readers": reader_report, "workers": worker_report, "delivery": delivery},
            503 if status == "down" else 200)
