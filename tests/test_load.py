"""The load test's own arithmetic and request handling (eval/run_load.py). Nothing is started or called."""

import pytest

from eval import run_load as load


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


def test_percentile_and_summary():
    assert load.percentile([], 0.95) == 0.0
    assert load.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5 and load.percentile(list(range(1, 101)), 0.95) == 95
    s = load.summarize([("ok", 1.0), ("ok", 3.0), ("rate limited", 0.1), ("HTTP 500", 9.0), ("HTTP 500", 9.0)], seconds=4.0)
    assert s["requests"] == 5 and s["ok"] == 2 and s["rate_limited"] == 1 and s["failed"] == {"HTTP 500": 2}
    assert s["per_second"] == 0.5 and s["median_s"] == 2.0 and s["slowest_s"] == 3.0      # timings of the ones that worked
    assert load.summarize([], 1.0)["median_s"] == 0.0


def test_a_level_counts_every_outcome_and_an_exception_is_an_outcome():
    def call(n):
        if n == 1:
            raise TimeoutError("slow")
        return "ok"
    level = load.run_level(call, workers=3, requests_each=4)
    assert level["callers"] == 3 and level["requests"] == 12 and level["ok"] == 8 and level["failed"] == {"TimeoutError": 4}
    with pytest.raises(ValueError):
        load.run_level(call, 1)
    with pytest.raises(ValueError):
        load.run_level(call, 1, seconds=1, requests_each=1)


def test_a_timed_level_stops_on_the_clock():
    now = [0.0]

    def clock():
        return now[0]

    def call(n):
        now[0] += 1.0
        return "ok"
    assert load.run_level(call, workers=1, seconds=5, clock=clock)["requests"] == 5


def test_gateway_call_reads_refusals_limits_and_stopped_questions():
    seen = []

    def post(url, json, headers, timeout):
        seen.append((url, headers["Authorization"]))
        return replies.pop(0)
    replies = [Reply(200, {"label": "allow"}), Reply(200, {"stopped": "decision", "reply": "..."}),
               Reply(200, {"error": "rate_limited"}), Reply(401, {"error": "unauthorized"}), Reply(200, {"error": "tool_failed"})]
    call = load.gateway_call("http://gw", ["t1", "t2"], post)
    assert [call(n) for n in range(5)] == ["ok", "ok", "rate limited", "HTTP 401", "tool_failed"]
    assert seen[0] == ("http://gw/check", "Bearer t1") and seen[1][1] == "Bearer t2" and seen[2][1] == "Bearer t1"


def test_chat_call_waits_for_the_answer_and_gives_up_on_time():
    now = [0.0]
    polls = [Reply(200, {"status": "pending"}), Reply(200, {"status": "done", "answer": "x"})]
    call = load.chat_call("http://api", "nurse", lambda url, json, timeout: Reply(200, {"request_id": "chat-1"}),
                          lambda url, timeout: polls.pop(0), wait_seconds=10,
                          sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert call(0) == "ok" and not polls
    never = load.chat_call("http://api", "nurse", lambda url, json, timeout: Reply(200, {"request_id": "chat-2"}),
                           lambda url, timeout: Reply(200, {"status": "pending"}), wait_seconds=1,
                           sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert never(0) == "no answer in 1 s"
    limited = load.chat_call("http://api", "nurse", lambda url, json, timeout: Reply(429), None, wait_seconds=1)
    assert limited(0) == "rate limited"
    assert load.chat_call("http://api", "nurse", lambda url, json, timeout: Reply(503), None, 1)(0) == "HTTP 503"


def test_table_and_the_reading_of_where_it_strained():
    fine = {"callers": 1, **load.summarize([("ok", 0.1)] * 10, 1.0)}
    slow = {"callers": 8, **load.summarize([("ok", 0.5)] * 10, 1.0)}
    broken = {"callers": 16, **load.summarize([("ok", 0.5)] * 8 + [("HTTP 500", 1.0)] * 2, 1.0)}
    assert "| 1 | 10 | 10 | 0 | 0 | 10.0 | 100.0 ms |" in load.table([fine]) and "2 HTTP 500" in load.table([broken])
    assert load.shown(0.0004) == "0.4 ms" and load.shown(2.5) == "2.50 s"
    assert load.first_strain([fine, slow, broken]).startswith("No failures, but at 8 at once")
    assert load.first_strain([fine, broken]).startswith("First failures at 16")
    assert load.first_strain([fine]).startswith("No failures and no level")


def test_the_questions_are_invented_and_include_one_the_checks_stop():
    assert any("dose" in q for q in load.QUESTIONS) and any("555-" in q for q in load.QUESTIONS)


def test_a_part_that_cannot_be_built_is_reported_and_the_rest_are_timed():
    def broken():
        raise ImportError("no such package")
    levels = load.component_levels([("Missing part", broken), ("Working part", lambda: (lambda i: i))], rounds=8)
    assert levels[0]["name"] == "Missing part" and levels[0]["failed"] == {"not available (ImportError)": 1}
    assert [lv["name"] for lv in levels[1:]] == ["Working part, 1 at once", "Working part, 4 at once"]
    assert levels[1]["ok"] == 8 and levels[2]["ok"] == 8
