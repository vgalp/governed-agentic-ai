"""The audit trail in files (AUDIT_SINK=file, ADR 039): same chain, no broker."""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from audit.chain import AUDIT_TOPICS, AuditChain, audit_producer, verify_chain
from ops.delivery import Delivery
from ops.filesink import FileProducer, read_events

ROOT = Path(__file__).resolve().parent.parent
KEY = b"test-key"


def chain(tmp_path, delivery=None):
    producer = FileProducer(tmp_path / "audit", delivery or Delivery())
    return AuditChain(producer, "gateway", KEY), producer


def test_events_are_written_one_per_line_and_the_chain_verifies(tmp_path):
    c, producer = chain(tmp_path)
    c.emit("audit.tool_calls", {"request_id": "r1", "tool": "search_records", "decision": "allow"}, flush=True)
    c.emit("audit.routing", {"request_id": "r1", "target": "local"})
    c.emit("audit.tool_calls", {"request_id": "r2", "tool": "search_records", "decision": "deny"})
    files = sorted(p.name for p in (tmp_path / "audit").iterdir())
    assert files == ["audit.routing.jsonl", "audit.tool_calls.jsonl"]
    assert len((tmp_path / "audit" / "audit.tool_calls.jsonl").read_text().splitlines()) == 2
    events = read_events(tmp_path / "audit")
    assert len(events) == 3 and verify_chain(events, KEY) == []
    assert producer.delivery.stats()["delivered"] == 3 and producer.flush() == 0


def test_a_changed_or_removed_line_is_found(tmp_path):
    c, _ = chain(tmp_path)
    for n in range(4):
        c.emit("audit.tool_calls", {"request_id": f"r{n}", "decision": "allow"})
    path = tmp_path / "audit" / "audit.tool_calls.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[1].replace('"allow"', '"deny"'), lines[3]]) + "\n")
    problems = verify_chain(read_events(tmp_path / "audit"), KEY)
    assert any("seq 1" in p for p in problems) and any("expected seq 2" in p for p in problems)


def test_files_are_private_and_only_audit_topics_are_kept(tmp_path):
    c, producer = chain(tmp_path)
    c.emit("audit.guardrails", {"request_id": "r1"})
    confirmed = []
    producer.produce("agent.trace", key="r1", value=json.dumps({"text": "a question someone typed"}),
                     on_delivery=lambda err, msg: confirmed.append(err))
    producer.produce("chat.responses", value="{}")
    directory = tmp_path / "audit"
    assert [p.name for p in directory.iterdir()] == ["audit.guardrails.jsonl"] and confirmed == [None]
    assert stat.S_IMODE((directory / "audit.guardrails.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_write_that_fails_is_a_lost_audit_event_not_a_crash(tmp_path):
    delivery = Delivery()
    c, producer = chain(tmp_path, delivery)
    (tmp_path / "audit" / "audit.tool_calls.jsonl").mkdir()            # the file cannot be opened for writing
    c.emit("audit.tool_calls", {"request_id": "r1"})
    assert delivery.stats()["audit_lost"] == 1 and delivery.stats()["failed"] == 1
    assert producer.send("audit.tool_calls", value="{}") is False
    assert producer.send("audit.routing", value="{}") is True


def test_the_sink_is_chosen_by_the_environment_and_a_typo_stops_the_process(tmp_path):
    producer = audit_producer("gateway", {"AUDIT_SINK": "file", "AUDIT_DIR": str(tmp_path / "a")})
    assert isinstance(producer, FileProducer) and producer.directory == tmp_path / "a"
    assert producer.topics == set(AUDIT_TOPICS)
    with pytest.raises(ValueError, match="AUDIT_SINK must be one of kafka, file"):
        audit_producer("gateway", {"AUDIT_SINK": "files"})


def test_the_verifier_reads_a_directory_and_reports_a_damaged_line(tmp_path):
    c, _ = chain(tmp_path)
    for n in range(3):
        c.emit("audit.tool_calls", {"request_id": f"r{n}", "profile": "clinic-oracle"})

    def verify():
        return subprocess.run([sys.executable, "-m", "audit.verify", "--dir", str(tmp_path / "audit"),
                               "--checkpoints", "none"], capture_output=True, text=True, cwd=ROOT,
                              env={"AUDIT_HMAC_KEY": KEY.decode(), "PATH": "/usr/bin:/bin"})
    ok = verify()
    assert ok.returncode == 0 and "Events read: 3" in ok.stdout, ok.stdout + ok.stderr
    with (tmp_path / "audit" / "audit.tool_calls.jsonl").open("a") as f:
        f.write('{"request_id": "r9", "audit": \n')
    bad = verify()
    assert bad.returncode == 1 and "audit.tool_calls.jsonl line 4: not a readable event" in bad.stdout
