"""Tamper-evident audit log.

Every audit event gets a sequence number and a hash that covers the event
itself and the hash of the event before it. Changing, deleting, inserting or
reordering any event breaks the chain, and `verify_chain` reports where.

If AUDIT_HMAC_KEY is set, hashes are HMAC-SHA256 with that key, so someone who
can edit the log but does not hold the key cannot recompute a valid chain.
Without a key, plain SHA-256 still detects accidental or careless changes.

Each event records which algorithm signed it ("hmac-sha256" or "sha256"). The
verifier does not take that field on trust: when a key is configured here, a
record that claims "sha256" is a problem, because anyone can rewrite a chain,
relabel it "sha256" and recompute plain hashes. Chains written before a key was
configured are checked only when the caller says so (allow_unkeyed=True), and
a chain that switches algorithm part way is always a problem.

Truncation (deleting the most recent events) leaves a valid chain by itself, so each chain also
writes signed checkpoints outside Kafka (ADR 030): every CHECKPOINT_EVERY events, at most a minute
after its latest event, and when the process exits, it appends {seq, hash} of its newest event to
its own file in AUDIT_CHECKPOINT_DIR (default logs/audit-checkpoints/). Each line is signed and
links to the line before it. `verify_checkpoints` then finds a chain cut short, rewritten, or gone.
A deployment points AUDIT_CHECKPOINT_DIR at storage the audit writers cannot rewrite (for example
object storage with a retention lock); on one machine the files are evidence, not a guarantee.
"""

import atexit
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from pathlib import Path

GENESIS = "0" * 64
# Every topic an AuditChain writes to. A topic missing here is never read by the verifier or the
# dashboard, so its events show as gaps in the chain (audit.input_agent was missing until 2026-11).
AUDIT_TOPICS = ["audit.guardrails", "audit.model_inputs", "audit.tool_calls", "audit.routing",
                "audit.approvals", "audit.input_agent"]


def _canonical(record: dict) -> bytes:
    """Same JSON bytes for the same content, whatever the key order."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_hash(record: dict, key: bytes | None) -> str:
    """Hash a record that does not yet contain its own hash."""
    data = _canonical(record)
    if key:
        return hmac.new(key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


CHECKPOINT_EVERY = 50          # events
CHECKPOINT_SECONDS = 60        # at most this long after a chain's newest event
SETTLE_SECONDS = 30            # the verifier ignores checkpoints younger than this (events still arriving)


def checkpoint_dir() -> Path:
    return Path(os.environ.get("AUDIT_CHECKPOINT_DIR") or Path(__file__).resolve().parent.parent / "logs" / "audit-checkpoints")


def key_from_env() -> bytes | None:
    value = os.environ.get("AUDIT_HMAC_KEY")
    return value.encode() if value else None


class Checkpoints:
    """Signed, linked checkpoint lines, one append-only file per chain (ADR 030)."""

    def __init__(self, directory: Path, key: bytes | None, clock=time.time):
        self.dir, self.key, self.clock = Path(directory), key, clock
        self._last: dict[str, tuple[int, str]] = {}          # chain -> (line number, signature)
        self._lock = threading.Lock()

    def write(self, chain_id: str, seq: int, head: str, profiles: set[str]) -> dict:
        with self._lock:
            n, prev = self._last.get(chain_id, (-1, GENESIS))
            line = {"chain_id": chain_id, "n": n + 1, "seq": seq, "hash": head, "events": seq + 1,
                    "profiles": sorted(profiles), "at": self.clock(), "prev": prev,
                    "alg": "hmac-sha256" if self.key else "sha256"}
            line["sig"] = compute_hash(line, self.key)
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"{chain_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._last[chain_id] = (line["n"], line["sig"])
            return line


def read_checkpoints(directory: Path | None = None) -> dict[str, list[dict]]:
    """Every checkpoint line, by chain. A line that is not JSON is kept as a problem marker."""
    out: dict[str, list[dict]] = {}
    d = Path(directory or checkpoint_dir())
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.jsonl")):
        lines = []
        for raw in f.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    lines.append({"unreadable": True})
        out[f.stem] = lines
    return out


class AuditChain:
    """Writes chained audit events. One chain per running process."""

    def __init__(self, producer, source: str, key: bytes | None = None, checkpoints: Checkpoints | None = None,
                 clock=time.time):
        self.producer = producer
        self.chain_id = f"{source}-{uuid.uuid4()}"
        self.key = key
        self.seq = 0
        self.prev_hash = GENESIS
        self._lock = threading.Lock()
        self.checkpoints, self.clock = checkpoints, clock
        self._profiles: set[str] = set()
        self._since_checkpoint = 0
        self._timer: threading.Timer | None = None
        if checkpoints is not None:
            atexit.register(self._checkpoint_at_exit)

    def _checkpoint_at_exit(self) -> None:
        try:
            self.checkpoint()
        except Exception as e:  # noqa: BLE001  exiting anyway; say why the last checkpoint is missing
            print(f"Audit: could not write the final checkpoint ({type(e).__name__})", flush=True)

    def checkpoint(self) -> dict | None:
        """Record the newest event's seq and hash outside Kafka. Nothing new since the last: nothing."""
        with self._lock:
            if self.checkpoints is None or self.seq == 0 or self._since_checkpoint == 0:
                return None
            self._since_checkpoint = 0
            return self.checkpoints.write(self.chain_id, self.seq - 1, self.prev_hash, set(self._profiles))

    def _after_emit(self) -> None:
        if self.checkpoints is None:
            return
        if self._since_checkpoint >= CHECKPOINT_EVERY:
            self.checkpoint()
        elif self._timer is None or not self._timer.is_alive():
            self._timer = threading.Timer(CHECKPOINT_SECONDS, self.checkpoint)
            self._timer.daemon = True
            self._timer.start()

    def emit(self, topic: str, event: dict, flush: bool = False) -> dict:
        with self._lock:
            record = {
                **event,
                "timestamp": event.get("timestamp", time.time()),
                "audit": {
                    "chain_id": self.chain_id,
                    "seq": self.seq,
                    "topic": topic,               # moving an event to another topic is detected too
                    "prev_hash": self.prev_hash,
                    "alg": "hmac-sha256" if self.key else "sha256",
                },
            }
            record["audit"]["hash"] = compute_hash(record, self.key)
            self.producer.produce(topic, key=event.get("request_id"), value=json.dumps(record))
            self.prev_hash = record["audit"]["hash"]
            self.seq += 1
            self._since_checkpoint += 1
            if isinstance(event.get("profile"), str):
                self._profiles.add(event["profile"])
        if flush:
            self.producer.flush()
        self._after_emit()
        return record


ALGS = ("hmac-sha256", "sha256")


def verify_chain(records: list[dict], key: bytes | None, allow_unkeyed: bool = False) -> list[str]:
    """Check chained audit records. Returns a list of problems; empty means intact.

    The algorithm is decided by the verifier, not by the record: with a key configured, every
    record must be keyed, unless allow_unkeyed=True (checking chains written before the key
    existed, on purpose). Without a key, keyed records cannot be checked and are reported.
    """
    problems = []
    chains: dict[str, list[dict]] = {}
    for r in records:
        chains.setdefault(r["audit"]["chain_id"], []).append(r)

    for chain_id, entries in chains.items():
        entries.sort(key=lambda r: r["audit"]["seq"])
        expected_seq, expected_prev = 0, GENESIS
        algs = {r["audit"].get("alg") for r in entries}
        if len(algs) > 1:
            problems.append(f"{chain_id}: mixes signing algorithms ({', '.join(sorted(map(str, algs)))}); "
                            "a chain is written with one, so this one was rewritten")
        for r in entries:
            a = r["audit"]
            if a["seq"] != expected_seq:
                problems.append(f"{chain_id}: expected seq {expected_seq}, found {a['seq']} (event missing or duplicated)")
            if a["prev_hash"] != expected_prev:
                problems.append(f"{chain_id} seq {a['seq']}: does not link to the previous event")
            unhashed = {**r, "audit": {k: v for k, v in a.items() if k != "hash"}}
            alg = a.get("alg")
            if alg not in ALGS:
                problems.append(f"{chain_id} seq {a['seq']}: unknown signing algorithm {alg!r}")
            elif alg == "hmac-sha256" and key is None:
                problems.append(f"{chain_id} seq {a['seq']}: signed with a key, but AUDIT_HMAC_KEY is not set here")
            elif alg == "sha256" and key is not None and not allow_unkeyed:
                problems.append(f"{chain_id} seq {a['seq']}: not signed with the key, though a key is configured "
                                "(a rewritten chain looks exactly like this)")
            elif compute_hash(unhashed, key if alg == "hmac-sha256" else None) != a["hash"]:
                problems.append(f"{chain_id} seq {a['seq']}: content does not match its hash (changed after writing)")
            expected_seq, expected_prev = a["seq"] + 1, a["hash"]
    return problems


def verify_checkpoints(records: list[dict], checkpoints: dict[str, list[dict]], key: bytes | None,
                       profile: str | None = None, now: float | None = None,
                       settle: float = SETTLE_SECONDS) -> tuple[list[str], dict]:
    """Compare the chains with their checkpoints. Finds a chain cut short (its newest checkpointed
    event is missing), rewritten (that event has another hash), or gone entirely, and a changed
    checkpoint file. `profile`: only chains that wrote for it. Returns (problems, summary)."""
    now = time.time() if now is None else now
    problems: list[str] = []
    by_chain: dict[str, dict[int, dict]] = {}
    for r in records:
        a = r["audit"]
        by_chain.setdefault(a["chain_id"], {})[a["seq"]] = r
    checked = 0
    for chain_id, lines in checkpoints.items():
        prev, ok_file = GENESIS, True
        for i, line in enumerate(lines):
            if line.get("unreadable") or line.get("n") != i or line.get("prev") != prev or line.get("chain_id") != chain_id:
                ok_file = False
                break
            alg = line.get("alg")
            if alg == "sha256" and key is not None:
                ok_file = False                       # anyone can sign a line without the key
                break
            unsigned = {k: v for k, v in line.items() if k != "sig"}
            if compute_hash(unsigned, key if alg == "hmac-sha256" else None) != line.get("sig"):
                ok_file = False
                break
            prev = line["sig"]
        if not ok_file:
            problems.append(f"{chain_id}: its checkpoint file was changed (line {i} does not verify)")
            continue
        settled = [x for x in lines if now - float(x["at"]) >= settle]
        if not settled:
            continue
        cp = settled[-1]
        if profile is not None and profile not in cp.get("profiles", []):
            continue
        checked += 1
        events = by_chain.get(chain_id)
        if not events:
            problems.append(f"{chain_id}: all {cp['events']} checkpointed events are missing (the chain was deleted, "
                            "or Kafka was reset; after a deliberate reset, archive the checkpoint directory)")
        elif cp["seq"] not in events:
            newest = max(events)
            problems.append(f"{chain_id}: events up to seq {cp['seq']} were checkpointed, but the chain ends at "
                            f"seq {newest} (its newest events were deleted)")
        elif events[cp["seq"]]["audit"]["hash"] != cp["hash"]:
            problems.append(f"{chain_id} seq {cp['seq']}: differs from its checkpoint (rewritten)")
    unchecked = len({c for c in by_chain if c not in checkpoints})
    return problems, {"chains_checked": checked, "chains_without_checkpoint": unchecked,
                      "checkpoint_files": len(checkpoints)}


def chains_of_profile(records: list[dict], profile: str) -> list[dict]:
    """Every record of each chain that wrote at least one event for this profile. Whole chains are
    kept, so a gap or a changed event inside them is still found."""
    wanted = {r["audit"]["chain_id"] for r in records if r.get("profile") == profile}
    return [r for r in records if r["audit"]["chain_id"] in wanted]


def unkeyed_chains(records: list[dict]) -> list[str]:
    """Chains written without a key: changes are detected, deliberate forgery is not."""
    return sorted({r["audit"]["chain_id"] for r in records if r["audit"].get("alg") != "hmac-sha256"})


_default_chain: AuditChain | None = None
_default_lock = threading.Lock()


def get_audit_chain(source: str) -> AuditChain:
    """The process-wide chain, so the planner and the gateway share one sequence."""
    global _default_chain
    with _default_lock:
        if _default_chain is None:
            from ops.delivery import Producer  # delivery confirmed per event (ADR 030)
            producer = Producer(service=source)
            key = key_from_env()
            if key is None:
                print("Audit: AUDIT_HMAC_KEY not set, using plain SHA-256 (detects changes, not deliberate forgery).")
            _default_chain = AuditChain(producer, source, key, Checkpoints(checkpoint_dir(), key))
        return _default_chain
