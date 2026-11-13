"""Connectivity to FHIR servers (ADR 036): circuit breaker, concurrency and rate limits, metrics,
response validation, time budgets, the per-session cache, the capability check, the connectivity
settings, and the shared session store.

No network and no real sleeping: fake sessions answer, clocks are injected.
Invented test data only.
"""

import json
import threading
import time

import pytest

from fhir.cache import ResponseCache
from fhir.capabilities import Capabilities, check_map, startup_check
from fhir.client import FhirClient
from fhir.errors import FhirDeadlineError, FhirError, FhirResponseError, FhirUnavailableError
from fhir.metrics import Metrics, resource_label
from fhir.resilience import BreakerState, CircuitBreaker, RateLimited, TokenBucket, budget, current_deadline, run_in_context
from fhir.sessions import FileBlobs, LaunchSession, MemoryBlobs, RedisBlobs, SessionStore, SessionStoreError, default_store
from fhir.transport import ServerPolicy, Transport, request_budget, reset_shared_transports, shared_transport
from profiles.fhir_map import FhirMappingError, parse
from profiles.loader import load_profile

BASE = "https://fhir.example.test/r4"
CAPS = {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"}


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body = status, body
        self.headers = {"Content-Type": "application/fhir+json", **(headers or {})}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class Server:
    """Answers each GET with `answer(url, params)`; counts calls."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {}), timeout))
        out = self.answer(url, params)
        if isinstance(out, Exception):
            raise out
        return out


def client(server, policy=None, auth=None, clock=None, **kw):
    metrics = Metrics()
    t = Transport(BASE, policy or ServerPolicy(), session=server, metrics=metrics,
                  clock=clock or time.monotonic, sleep=(clock.sleep if clock else time.sleep))
    return FhirClient(BASE, auth, transport=t, sleep=(clock.sleep if clock else (lambda s: None)), **kw), metrics


def bundle(resources, **extra):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources], **extra}


# --- 1. circuit breaker ---------------------------------------------------------------------------------------

def test_the_breaker_opens_after_a_run_of_failures_and_then_fails_at_once():
    clock = Clock()
    server = Server(lambda u, p: Resp(503, {}))
    c, metrics = client(server, ServerPolicy(retries=0, breaker_failures=3, breaker_reset_seconds=30), clock=clock)
    for _ in range(3):
        with pytest.raises(FhirError, match="503"):
            c.capability()
    with pytest.raises(FhirUnavailableError, match="not answering"):
        c.capability()
    assert len(server.calls) == 3                                   # the fourth never reached the server
    assert metrics.value("fhir_breaker_open", server="fhir.example.test") == 1.0
    assert metrics.value("fhir_rejected_total", server="fhir.example.test", reason="breaker") == 1


def test_after_the_pause_one_trial_call_closes_the_breaker_when_it_succeeds():
    clock = Clock()
    answers = iter([Resp(503, {})] * 2 + [Resp(200, CAPS)])
    c, metrics = client(Server(lambda u, p: next(answers)), ServerPolicy(retries=0, breaker_failures=2,
                                                                          breaker_reset_seconds=30), clock=clock)
    for _ in range(2):
        with pytest.raises(FhirError):
            c.capability()
    clock.t += 31
    assert c.capability()["resourceType"] == "CapabilityStatement"
    assert c.transport.breaker.state is BreakerState.CLOSED
    assert metrics.value("fhir_breaker_open", server="fhir.example.test") == 0.0


def test_a_failed_trial_reopens_the_breaker_and_only_one_trial_runs_at_a_time():
    clock = Clock()
    b = CircuitBreaker("s", failures=1, reset_seconds=10, clock=clock)
    b.record(False)
    assert b.state is BreakerState.OPEN and not b.allow()
    clock.t += 10
    assert b.allow() and not b.allow()                              # the trial, then everyone else waits
    b.record(False)
    assert b.state is BreakerState.OPEN and b.retry_in() == 10


def test_a_trial_that_never_went_out_lets_the_next_caller_try():
    clock = Clock()
    b = CircuitBreaker("s", failures=1, reset_seconds=10, clock=clock)
    b.record(False)
    clock.t += 10
    assert b.allow()
    b.abandon()
    assert b.allow()


def test_refusals_and_busy_answers_do_not_count_against_the_breaker():
    answers = iter([Resp(404, {})] * 5 + [Resp(429, {})] * 5 + [Resp(200, CAPS)])
    c, _ = client(Server(lambda u, p: next(answers)), ServerPolicy(retries=0, breaker_failures=2))
    for _ in range(10):
        with pytest.raises(FhirError):
            c.capability()
    assert c.capability() and c.transport.breaker.state is BreakerState.CLOSED


# --- 2. connection pooling and concurrency --------------------------------------------------------------------

def test_no_more_than_max_concurrent_requests_are_in_flight():
    inflight, peak, lock, gate = [0], [0], threading.Lock(), threading.Event()

    def answer(u, p):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        gate.wait(2)
        with lock:
            inflight[0] -= 1
        return Resp(200, CAPS)
    c, _ = client(Server(answer), ServerPolicy(max_concurrent=2))
    threads = [threading.Thread(target=c.capability) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert peak[0] == 2


def test_a_request_that_cannot_get_a_slot_within_its_budget_fails_fast():
    gate = threading.Event()
    c, metrics = client(Server(lambda u, p: (gate.wait(5), Resp(200, CAPS))[1]), ServerPolicy(max_concurrent=1))
    first = threading.Thread(target=c.capability)
    first.start()
    time.sleep(0.1)
    started = time.monotonic()
    with budget(0.3), pytest.raises(FhirDeadlineError, match="already waiting"):
        c.capability()
    assert time.monotonic() - started < 1.5
    gate.set()
    first.join(5)
    assert metrics.value("fhir_rejected_total", server="fhir.example.test", reason="queue") == 1


def test_clients_of_one_server_share_one_transport_and_its_connection_pool():
    reset_shared_transports()
    try:
        policy = ServerPolicy(pool_size=6)
        a, b = FhirClient(BASE, policy=policy), FhirClient(BASE + "/", policy=policy)
        assert a.transport is b.transport is shared_transport(BASE, policy)
        adapter = a.session.get_adapter(BASE)
        assert adapter._pool_maxsize == 6 and adapter.max_retries.total == 0
        assert FhirClient(BASE, session=Server(lambda u, p: None)).transport is not a.transport   # a test double: its own
    finally:
        reset_shared_transports()


# --- 3. client-side rate limiting -----------------------------------------------------------------------------

def test_the_token_bucket_allows_a_burst_then_the_rate():
    clock = Clock()
    bucket = TokenBucket(rate=2, burst=3, clock=clock, sleep=clock.sleep)
    assert [bucket.acquire(10) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.acquire(10) == pytest.approx(0.5)                # one every half second after the burst
    b = TokenBucket(rate=1, burst=1, clock=clock, sleep=clock.sleep)
    b.acquire(0)
    with pytest.raises(RateLimited):
        b.acquire(0.5)                                              # the next token is a second away


def test_the_transport_waits_for_the_rate_limit_and_refuses_past_the_budget():
    clock = Clock()
    server = Server(lambda u, p: Resp(200, CAPS))
    c, metrics = client(server, ServerPolicy(rate_per_second=1, rate_burst=1), clock=clock)
    c.capability()
    c.capability()                                                  # waited one (fake) second
    assert clock.t == pytest.approx(1001.0)
    with budget(0.5, clock=clock), pytest.raises(FhirDeadlineError, match="rate limit"):
        c.capability()
    assert len(server.calls) == 2
    assert metrics.value("fhir_rejected_total", server="fhir.example.test", reason="rate") == 1


# --- 4. metrics -----------------------------------------------------------------------------------------------

def test_every_attempt_is_counted_by_outcome_and_timed():
    import requests
    answers = iter([requests.ReadTimeout("slow"), Resp(503, {}), Resp(200, bundle([]))])
    c, metrics = client(Server(lambda u, p: next(answers)), jitter=None)
    c.search("Condition", {"patient": "p1"})
    labels = {"server": "fhir.example.test", "resource": "Condition"}
    assert [metrics.value("fhir_requests_total", outcome=o, **labels) for o in ("timeout", "5xx", "ok")] == [1, 1, 1]
    assert metrics.count("fhir_request_seconds", **labels) == 2     # the timeout got no answer to time
    assert metrics.total("fhir_retries_total", server="fhir.example.test") == 2


def test_a_metric_label_is_a_resource_type_never_an_id():
    assert resource_label(BASE, f"{BASE}/Patient/eAbC.123") == "Patient"
    assert resource_label(BASE, f"{BASE}/metadata") == "metadata"
    assert resource_label(BASE, f"{BASE}/patient-id-in-path") == "other"
    assert resource_label(BASE, "https://elsewhere.test/x/12724065") == "other"


def test_metrics_are_served_in_prometheus_text_format():
    m = Metrics()
    m.inc("fhir_requests_total", server="h", resource="Patient", outcome="ok")
    m.observe("fhir_request_seconds", 0.3, server="h", resource="Patient")
    m.set("fhir_breaker_open", 1, server='h"x')
    text = m.prometheus_text()
    assert '# TYPE fhir_requests_total counter' in text
    assert 'fhir_requests_total{outcome="ok",resource="Patient",server="h"} 1' in text
    assert 'fhir_request_seconds_bucket{resource="Patient",server="h",le="0.25"} 0' in text
    assert 'fhir_request_seconds_bucket{resource="Patient",server="h",le="0.5"} 1' in text
    assert 'fhir_request_seconds_count{resource="Patient",server="h"} 1' in text
    assert 'server="h_x"' in text                                   # a label value cannot break the format


def test_each_tool_server_exposes_metrics_at_a_route():
    from mcp_servers.common.serve import add_metrics_route

    class FakeMcp:
        routes = {}

        def custom_route(self, path, methods, name=None, include_in_schema=True):
            def register(fn):
                self.routes[path] = (methods, fn)
                return fn
            return register
    mcp = FakeMcp()
    add_metrics_route(mcp)
    methods, handler = mcp.routes["/metrics"]
    import asyncio
    response = asyncio.run(handler(None))
    assert methods == ["GET"] and response.media_type.startswith("text/plain")


# --- 5. response validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("answer, message", [
    (Resp(200, "<html>proxy error</html>", {"Content-Type": "text/html"}), "text/html"),
    (Resp(200, "{truncated"), "not valid JSON"),
    (Resp(200, ["a", "list"]), "not a JSON object"),
    (Resp(200, {"resourceType": "OperationOutcome"}), "not a Bundle"),
    (Resp(200, {"resourceType": "Bundle", "type": "transaction"}), "type 'transaction'"),
    (Resp(200, bundle([{"resourceType": "Patient", "id": "x"}])), "a Patient as a match"),
    (Resp(200, {"resourceType": "Bundle", "type": "searchset", "entry": [{"fullUrl": "x"}]}), "without a resource"),
    (Resp(200, bundle([], total=-1)), "invalid total"),
])
def test_a_malformed_search_answer_fails_loudly(answer, message):
    c, metrics = client(Server(lambda u, p: answer))
    with pytest.raises(FhirResponseError, match=message):
        c.search("Condition", {"patient": "p1"})
    assert metrics.value("fhir_response_invalid_total", server="fhir.example.test", resource="Condition") == 1


def test_a_read_must_return_the_resource_asked_for():
    c, _ = client(Server(lambda u, p: Resp(200, {"resourceType": "Patient", "id": "someone-else"})))
    with pytest.raises(FhirResponseError, match="another id"):
        c.read("Patient", "p1")
    c, _ = client(Server(lambda u, p: Resp(200, {"resourceType": "Practitioner", "id": "p1"})))
    with pytest.raises(FhirResponseError, match="returned a Practitioner"):
        c.read("Patient", "p1")


def test_included_resources_are_skipped_and_an_error_outcome_marks_the_list_incomplete():
    body = {"resourceType": "Bundle", "type": "searchset", "entry": [
        {"resource": {"resourceType": "MedicationRequest", "id": "m1"}, "search": {"mode": "match"}},
        {"resource": {"resourceType": "Medication", "id": "x"}, "search": {"mode": "include"}},
        {"resource": {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "too-costly",
                                                                     "diagnostics": "partial result"}]},
         "search": {"mode": "outcome"}}]}
    c, _ = client(Server(lambda u, p: Resp(200, body)))
    found, more = c.search_all("MedicationRequest", {"patient": "p1"})
    assert [r["id"] for r in found] == ["m1"] and more is True


def test_a_warning_outcome_does_not_mark_the_list_incomplete():
    body = bundle([{"resourceType": "Condition", "id": "c1"}])
    body["entry"].append({"resource": {"resourceType": "OperationOutcome", "issue": [{"severity": "warning"}]},
                          "search": {"mode": "outcome"}})
    c, _ = client(Server(lambda u, p: Resp(200, body)))
    assert c.search_all("Condition", {"patient": "p1"}) == ([{"resourceType": "Condition", "id": "c1"}], False)


# --- 6. time budget -------------------------------------------------------------------------------------------

def test_nested_budgets_keep_the_earlier_deadline():
    clock = Clock()
    with budget(10, clock=clock) as outer:
        with budget(60, clock=clock) as inner:
            assert inner is outer
        with budget(2, clock=clock) as shorter:
            assert shorter.expires_at == 1002
    assert current_deadline() is None


def test_a_spent_budget_sends_nothing_and_each_timeout_is_cut_to_what_is_left():
    clock = Clock()
    server = Server(lambda u, p: Resp(200, CAPS))
    c, _ = client(server, ServerPolicy(timeout=45), clock=clock)
    with budget(10, clock=clock):
        clock.t += 4
        c.capability()
        assert server.calls[-1][3] == pytest.approx(6)              # 45 s timeout, 6 s of budget left
        clock.t += 6
        with pytest.raises(FhirDeadlineError, match="ran out"):
            c.capability()
    assert len(server.calls) == 1


def test_a_retry_wait_longer_than_what_is_left_is_not_waited():
    clock = Clock()
    server = Server(lambda u, p: Resp(429, {}, {"Retry-After": "8"}))
    c, _ = client(server, clock=clock)
    with budget(5, clock=clock), pytest.raises(FhirError, match="429"):
        c.capability()
    assert len(server.calls) == 1 and clock.t == 1000.0


def test_the_budget_reaches_threads_started_for_parallel_reads():
    from concurrent.futures import ThreadPoolExecutor
    with budget(30) as d, ThreadPoolExecutor(2) as pool:
        seen = pool.submit(run_in_context(current_deadline)).result()
        plain = pool.submit(current_deadline).result()
    assert seen is d and plain is None


def test_a_tool_call_runs_within_the_profile_budget():
    server = load_profile("clinic-oracle").fhir_map.server
    with request_budget(server) as d:
        assert 84 < d.remaining() <= 85
    with request_budget(None) as nothing:
        assert nothing is None and current_deadline() is None


# --- 7. per-session cache -------------------------------------------------------------------------------------

class SessionAuth:
    def __init__(self, scope):
        self.cache_scope, self.invalidated = scope, 0

    def token(self):
        return "t"

    def invalidate(self):
        self.invalidated += 1


def test_a_person_s_second_read_within_the_ttl_comes_from_the_cache_and_another_person_s_does_not():
    server = Server(lambda u, p: Resp(200, bundle([{"resourceType": "Condition", "id": "c1"}])))
    metrics = Metrics()
    t = Transport(BASE, ServerPolicy(cache_seconds=60), session=server, metrics=metrics)
    alice, bob = FhirClient(BASE, SessionAuth("launch:a"), transport=t), FhirClient(BASE, SessionAuth("launch:b"), transport=t)
    alice.search("Condition", {"patient": "p1"})
    alice.search("Condition", {"patient": "p1"})
    bob.search("Condition", {"patient": "p1"})
    assert len(server.calls) == 2
    assert metrics.value("fhir_cache_total", result="hit") == 1


def test_the_backend_sign_in_is_never_cached():
    server = Server(lambda u, p: Resp(200, bundle([])))
    t = Transport(BASE, ServerPolicy(cache_seconds=60), session=server, metrics=Metrics())
    backend = FhirClient(BASE, None, transport=t)            # the app's own identity: no cache scope
    for _ in range(2):
        backend.search("Condition", {"patient": "p1"})
    assert len(server.calls) == 2 and len(t.cache) == 0


def test_cached_answers_expire_are_bounded_and_are_copies():
    clock = Clock()
    cache = ResponseCache(ttl=60, max_entries=2, clock=clock, metrics=Metrics())
    cache.put("s", "u1", None, {"a": [1]})
    got = cache.get("s", "u1", None)
    got["a"].append(2)
    assert cache.get("s", "u1", None) == {"a": [1]}
    cache.put("s", "u2", None, {})
    cache.put("s", "u3", None, {})
    assert cache.get("s", "u1", None) is None and len(cache) == 2  # least recently used went first
    clock.t += 61
    assert cache.get("s", "u3", None) is None
    with pytest.raises(ValueError):
        ResponseCache(ttl=600)


def test_a_refused_token_clears_that_session_s_cache_and_errors_are_not_cached():
    answers = iter([Resp(200, bundle([])), Resp(401, {}), Resp(500, {}), Resp(200, bundle([]))])
    server = Server(lambda u, p: next(answers))
    t = Transport(BASE, ServerPolicy(cache_seconds=60, retries=0), session=server, metrics=Metrics())
    auth = SessionAuth("launch:a")
    c = FhirClient(BASE, auth, transport=t)
    c.search("Condition", {"patient": "p1"})                        # read, and cached
    with pytest.raises(FhirError, match="500"):
        c.search("Condition", {"patient": "p2"})                    # 401: signed in again, cache dropped; then 500
    assert auth.invalidated == 1 and len(t.cache) == 0
    c.search("Condition", {"patient": "p1"})                        # no longer cached: read again
    assert len(server.calls) == 4


# --- 8. capability check --------------------------------------------------------------------------------------

def statement(**resources):
    return {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1", "rest": [{"mode": "server", "resource": [
        {"type": t, "searchParam": [{"name": n} for n in params]} for t, params in resources.items()]}]}


ORACLE = load_profile("clinic-oracle")
ORACLE_MAP = ORACLE.fhir_map
EVERYTHING = {"Patient": ["given", "family", "birthdate"], "MedicationRequest": ["patient", "status"],
              "Encounter": ["patient", "date"], "Condition": ["patient", "category", "clinical-status"],
              "AllergyIntolerance": ["patient", "clinical-status"], "Observation": ["patient", "category", "date"]}


def test_a_server_that_declares_everything_raises_no_finding():
    assert check_map(Capabilities.parse(statement(**EVERYTHING)), ORACLE_MAP) == []


def test_a_missing_resource_type_is_an_error_and_an_undeclared_date_window_a_warning():
    caps = {**EVERYTHING, "Encounter": ["patient"]}
    del caps["AllergyIntolerance"]
    findings = {f.level: f.message for f in check_map(Capabilities.parse(statement(**caps)), ORACLE_MAP)}
    assert set(findings) == {"error", "warning"}
    assert "AllergyIntolerance is not offered" in findings["error"]
    assert "'date'" in findings["warning"] and "date window may not be applied" in findings["warning"]


def test_a_resource_listing_no_parameters_is_not_reported_and_another_fhir_version_is():
    caps = Capabilities.parse({**statement(**{k: [] for k in EVERYTHING}), "fhirVersion": "3.0.2"})
    assert [f.level for f in check_map(caps, ORACLE_MAP)] == ["info"]


def test_the_startup_check_never_raises_when_the_server_is_down():
    c, _ = client(Server(lambda u, p: Resp(503, {})), ServerPolicy(retries=0))
    findings = startup_check(c, ORACLE_MAP, "test")
    assert findings[0].level == "info" and "not checked" in findings[0].message


# --- 9. connectivity settings ---------------------------------------------------------------------------------

def mapping_with(**server):
    """The Oracle Health mapping with its connectivity settings replaced."""
    import yaml
    raw = yaml.safe_load(open("profiles/clinic-oracle/fhir.yaml"))
    keep = {k: v for k, v in raw["server"].items()
            if k not in ("max_concurrent", "rate_limit", "breaker", "budget", "cache_seconds")}
    raw["server"] = {**keep, **server}
    return parse(raw, "test", set(ORACLE.data_classes))


def test_the_profiles_connectivity_settings_become_the_policy():
    p = ServerPolicy.from_settings(ORACLE_MAP.server)
    assert (p.timeout, p.max_concurrent, p.rate_per_second, p.rate_burst, p.breaker_failures, p.budget_seconds,
            p.cache_seconds) == (45, 4, 10, 20, 5, 85, 60)
    assert ServerPolicy.from_settings({}) == ServerPolicy()


@pytest.mark.parametrize("server, message", [
    ({"max_concurrent": 0}, "max_concurrent"),
    ({"cache_seconds": 600}, "cache_seconds"),
    ({"rate_limit": {"burst": 3}}, "needs per_second"),
    ({"rate_limit": {"per_second": 5, "speed": 1}}, "rate_limit takes"),
    ({"breaker": {"failures": 0}}, "breaker.failures"),
    ({"budget": 30, "timeout": 45}, "longer than timeout"),
    ({"retries": True}, "retries"),
])
def test_an_unsafe_connectivity_setting_is_refused(server, message):
    with pytest.raises(FhirMappingError, match=message):
        mapping_with(**server)


# --- 10. shared session store ---------------------------------------------------------------------------------

def session(sid="sid-1", ends_at=2000.0):
    return LaunchSession(id=sid, iss=BASE, patient="p1", encounter=None, user="Practitioner/x", name="Dr X",
                         role="clinician", access_token="secret-token", refresh_token=None, expires_at=1500.0,
                         scope="launch openid", token_endpoint="https://auth.example.test/token", created=1000.0,
                         ends_at=ends_at)


class FakeRedis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v, ex=None):
        self.data[k], self.ttl[k] = v, ex

    def delete(self, k):
        self.data.pop(k, None)

    def scan_iter(self, match=None, count=None):
        prefix = (match or "*").rstrip("*")
        return [k.encode() for k in self.data if k.startswith(prefix)]


@pytest.mark.parametrize("make", [MemoryBlobs, lambda: FileBlobs(_tmp()), lambda: RedisBlobs(FakeRedis(), "gaai:t:")])
def test_every_backend_keeps_sessions_the_same_way(make):
    clock = Clock()
    store = SessionStore(SessionStore.new_key(), backend=make(), clock=clock)
    store.save(session())
    assert store.load("sid-1").access_token == "secret-token"
    assert store.load_ref(store.ref("sid-1")).patient == "p1"
    store.save(session("sid-2", ends_at=1001.0))
    clock.t = 1500
    assert store.sweep() == 1 and store.load("sid-1") is not None
    store.delete("sid-1")
    assert store.load("sid-1") is None


def _tmp():
    import tempfile
    return tempfile.mkdtemp()


def test_redis_holds_only_ciphertext_under_hashed_names_and_expires_with_the_session():
    redis = FakeRedis()
    store = SessionStore(SessionStore.new_key(), backend=RedisBlobs(redis, "gaai:clinic-epic:"), clock=Clock())
    store.save(session())
    (key, blob), = redis.data.items()
    assert key == "gaai:clinic-epic:" + store.ref("sid-1") and "sid-1" not in key
    assert b"secret-token" not in blob
    assert redis.ttl[key] == 1000                                    # ends_at - now


def test_a_redis_without_tls_on_another_machine_is_refused():
    with pytest.raises(SessionStoreError, match="rediss://"):
        RedisBlobs.from_url("redis://cache.example.test:6379/0")
    with pytest.raises(SessionStoreError, match="redis:// or rediss://"):
        RedisBlobs.from_url("memcached://localhost")


def test_a_redis_that_does_not_answer_is_a_store_error_not_a_crash():
    class Down(FakeRedis):
        def get(self, k):
            raise ConnectionError("refused")
    store = SessionStore(SessionStore.new_key(), backend=RedisBlobs(Down()), clock=Clock())
    with pytest.raises(SessionStoreError, match="did not answer"):
        store.load("sid-1")


def test_the_default_store_is_a_folder_unless_a_shared_store_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("GAAI_SESSION_KEY_REF", "env:TEST_KEY")
    monkeypatch.setenv("TEST_KEY", SessionStore.new_key().decode())
    store = default_store("clinic-epic", environ={"GAAI_SESSIONS_DIR": str(tmp_path)})
    assert isinstance(store.backend, FileBlobs) and store.folder == tmp_path / "clinic-epic"
    with pytest.raises(SessionStoreError, match="rediss://"):
        default_store("clinic-epic", environ={"GAAI_SESSIONS_URL": "redis://cache.example.test:6379/0"})
