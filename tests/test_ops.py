"""Delivery, liveness and supervision (audit OC-2, OC-3; ADR 030): a message the broker did not take
is noticed, a person is not handed a request ID nothing will answer, a silent or stuck worker shows
on /health, and a crashing service is restarted, then given up on and reported."""

import json

import pytest

from ops import health, supervise
from ops.delivery import Delivery, Producer


class RawProducer:
    """Stands in for confluent_kafka.Producer: delivery callbacks run on flush, with the given error."""

    def __init__(self, error=None, stuck=False):
        self.error, self.stuck, self.pending = error, stuck, []

    def produce(self, topic, key=None, value=None, on_delivery=None, **kw):
        self.pending.append((topic, on_delivery))

    def poll(self, timeout=0):
        return 0

    def flush(self, timeout=10):
        if self.stuck:
            return len(self.pending)
        for topic, cb in self.pending:
            cb(self.error, None)
        self.pending = []
        return 0


def test_a_confirmed_message_is_counted_and_send_says_so():
    d = Delivery()
    p = Producer(delivery=d, raw=RawProducer())
    assert p.send("chat.requests", "r1", "{}") is True
    assert d.stats()["delivered"] == 1 and d.stats()["failed"] == 0


def test_a_refused_message_is_counted_and_send_returns_false(capsys):
    d = Delivery()
    p = Producer(delivery=d, raw=RawProducer(error="Broker: Not enough in-sync replicas"))
    assert p.send("chat.requests", "r1", "{}") is False
    s = d.stats()
    assert s["failed"] == 1 and s["recent_failures"] == 1 and s["last_failure"]["topic"] == "chat.requests"
    assert "{}" not in capsys.readouterr().out          # the content is never logged


def test_a_lost_audit_event_is_reported_as_a_gap(capsys):
    d = Delivery()
    p = Producer(delivery=d, raw=RawProducer(error="timed out"))
    p.produce("audit.tool_calls", "r1", "{}")
    p.flush()
    assert d.stats()["audit_lost"] == 1
    assert "AUDIT EVENT LOST" in capsys.readouterr().out


def test_a_flush_that_times_out_is_not_confirmed():
    d = Delivery()
    p = Producer(delivery=d, raw=RawProducer(stuck=True))
    assert p.send("approvals.decided", "a1", "{}", timeout=0.1) is False
    assert d.stats()["unconfirmed"] == 1


def test_old_failures_stop_counting_as_recent():
    now = [1000.0]
    d = Delivery(clock=lambda: now[0])
    d.record("chat.requests", "x")
    assert d.stats()["recent_failures"] == 1
    now[0] += 301
    assert d.stats()["recent_failures"] == 0 and d.stats()["failed"] == 1


def test_a_callers_own_callback_still_runs():
    seen, raw = [], RawProducer()
    Producer(delivery=Delivery(), raw=raw).produce("t", "k", "v", on_delivery=lambda e, m: seen.append(e))
    raw.flush()
    assert seen == [None]


# --- heartbeats -----------------------------------------------------------------------------------

def test_heartbeats_say_ok_silent_stuck_gave_up_or_missing(tmp_path):
    now = [100.0]
    clock = lambda: now[0]   # noqa: E731
    hb = health.Heartbeat("planner", tmp_path, clock)
    hb.beat()
    assert health.read("planner", tmp_path, clock)["status"] == "ok"
    now[0] += health.STALE_SECONDS + 1                   # idle and silent: the process is gone or hung
    assert health.read("planner", tmp_path, clock)["status"] == "silent"
    hb.working("a chat request")                         # a long request is not a dead worker
    now[0] += 600
    assert health.read("planner", tmp_path, clock)["status"] == "ok"
    now[0] += health.WORK_LIMIT                          # but past the limit it is stuck
    assert health.read("planner", tmp_path, clock)["status"] == "stuck"
    health.Heartbeat("planner.supervisor", tmp_path, clock).set("gave_up", restarts=4)
    assert health.read("planner.supervisor", tmp_path, clock)["status"] == "gave_up"
    assert health.read("executor", tmp_path, clock)["status"] == "missing"


def test_an_idle_worker_writes_at_most_every_few_seconds(tmp_path):
    now = [0.0]
    hb = health.Heartbeat("executor", tmp_path, lambda: now[0])
    hb.beat()
    first = json.loads((tmp_path / "executor.json").read_text())["at"]
    now[0] += 1
    hb.beat()
    assert json.loads((tmp_path / "executor.json").read_text())["at"] == first
    now[0] += health.BEAT_SECONDS
    hb.beat()
    assert json.loads((tmp_path / "executor.json").read_text())["at"] == now[0]


def _assess(broker=(True, None), readers=None, workers=None, supervisors=None, delivery=None):
    return health.assess(broker, readers if readers is not None else {"responses": (True, 1.0)},
                         workers if workers is not None else {"planner": {"name": "planner", "status": "ok"}},
                         supervisors or {}, delivery or {"audit_lost": 0, "recent_failures": 0})


def test_health_is_ok_when_everything_answers():
    body, code = _assess()
    assert body["status"] == "ok" and code == 200 and not body["warnings"]


@pytest.mark.parametrize("kwargs", [{"broker": (False, "KafkaException")},
                                    {"readers": {"responses": (False, 1.0)}},
                                    {"readers": {"responses": (True, 120.0)}}])
def test_health_is_down_without_a_broker_or_with_a_dead_reader(kwargs):
    body, code = _assess(**kwargs)
    assert body["status"] == "down" and code == 503 and body["problems"]


@pytest.mark.parametrize("kwargs, words", [
    ({"workers": {"planner": {"name": "planner", "status": "silent"}}}, "planner: silent"),
    ({"supervisors": {"planner": {"status": "gave_up", "restarts": 4}}}, "planner: gave_up"),
    ({"delivery": {"audit_lost": 2, "recent_failures": 0}}, "audit chain has a gap"),
    ({"delivery": {"audit_lost": 0, "recent_failures": 3}}, "delivery failure"),
])
def test_health_is_degraded_when_something_needs_attention(kwargs, words):
    body, code = _assess(**kwargs)
    assert body["status"] == "degraded" and code == 200
    assert any(words in w for w in body["warnings"])


def test_reader_ticks():
    now = [50.0]
    health.tick("responses", lambda: now[0])
    now[0] += 3
    assert health.tick_age("responses", lambda: now[0]) == 3
    assert health.tick_age("never-started") is None


# --- the supervisor -------------------------------------------------------------------------------

class Child:
    def __init__(self, code):
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def poll(self):
        return self.code


def test_a_crashing_service_is_restarted_with_growing_waits_then_given_up(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    started, waits = [], []

    def popen(cmd):
        started.append(cmd)
        return Child(1)
    code = supervise.run("planner", ["agents.planner"], popen=popen, sleep=waits.append, clock=lambda: 0.0)
    assert code == 1 and len(started) == supervise.MAX_CRASHES
    assert started[0][1:] == ["-m", "agents.planner"]
    assert waits == [1, 2, 4, 8]                          # 1, 2, 4 ... seconds between quick crashes
    state = health.read("planner.supervisor", tmp_path, lambda: 0.0)
    assert state["status"] == "gave_up" and state["last_exit"] == 1


def test_crashes_far_apart_do_not_add_up(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "HEALTH_DIR", tmp_path)
    t, runs = [0.0], []

    def popen(cmd):
        runs.append(t[0])
        if len(runs) == 8:
            raise KeyboardInterrupt                       # stop the test after eight runs
        t[0] += supervise.WINDOW                          # each run lasts as long as the window
        return Child(1)
    with pytest.raises(KeyboardInterrupt):
        supervise.run("executor", ["agents.executor"], popen=popen, sleep=lambda s: None, clock=lambda: t[0])
    assert len(runs) == 8                                 # never gave up: one crash per window


def test_backoff_is_capped():
    assert [supervise.backoff(n) for n in (1, 2, 3, 7, 20)] == [1, 2, 4, 60, 60]


def test_the_supervisor_explains_its_usage():
    assert supervise.main(["planner"]) == 2


# --- rate limit -----------------------------------------------------------------------------------

from ops.ratelimit import RateLimiter  # noqa: E402


def test_a_burst_then_the_steady_rate():
    now = [0.0]
    r = RateLimiter(per_minute=6, burst=3, clock=lambda: now[0])
    assert [r.allow("user:a")[0] for _ in range(4)] == [True, True, True, False]
    allowed, wait = r.allow("user:a")
    assert not allowed and wait == pytest.approx(10.0)          # 6 a minute: one every 10 s
    now[0] += 10
    assert r.allow("user:a")[0] and not r.allow("user:a")[0]


def test_one_person_does_not_use_up_anothers_messages():
    r = RateLimiter(per_minute=1, burst=1, clock=lambda: 0.0)
    assert r.allow("user:a")[0] and not r.allow("user:a")[0]
    assert r.allow("user:b")[0]


def test_memory_is_bounded():
    r = RateLimiter(per_minute=1, burst=1, clock=lambda: 0.0, max_keys=3)
    for k in "abcde":
        r.allow(k)
    assert list(r._buckets) == ["c", "d", "e"]


def test_the_profile_sets_the_limit_and_bad_values_are_refused():
    class P:
        rate_limit = {"per_minute": 5, "burst": 2}
    r = RateLimiter.for_profile(P)
    assert r.burst == 2 and r.rate == pytest.approx(5 / 60)
    assert RateLimiter.for_profile(object()).burst == 10      # defaults: 20 a minute, 10 at once
    with pytest.raises(ValueError):
        RateLimiter(per_minute=0)
