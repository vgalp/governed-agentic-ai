"""Verify the audit trail.

  uv run python -m audit.verify                      # read the audit topics from Kafka and check them
  uv run python -m audit.verify --export audit.jsonl # also save what was read, one event per line
  uv run python -m audit.verify --file audit.jsonl   # check a saved file instead of Kafka

Exit code 0 = intact, 1 = problems found.
"""

import argparse
import json
import sys
import time
import uuid

from audit.chain import AUDIT_TOPICS, key_from_env, unkeyed_chains, verify_chain


def read_from_kafka(idle_seconds: float = 5.0) -> list[dict]:
    from confluent_kafka import Consumer
    consumer = Consumer({
        "bootstrap.servers": "localhost:9092",
        "group.id": f"audit-verify-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe(AUDIT_TOPICS)
    records, last_message = [], time.time()
    while time.time() - last_message < idle_seconds:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        records.append(json.loads(msg.value()))
        last_message = time.time()
    consumer.close()
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the tamper-evident audit trail.")
    parser.add_argument("--file", help="verify a saved JSONL file instead of reading Kafka")
    parser.add_argument("--export", help="save the events read from Kafka to this JSONL file")
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            records = [json.loads(line) for line in f if line.strip()]
    else:
        records = read_from_kafka()
        if args.export:
            with open(args.export, "w") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

    chained = [r for r in records if "audit" in r]
    legacy = len(records) - len(chained)
    problems = verify_chain(chained, key_from_env())
    chains = {r["audit"]["chain_id"] for r in chained}

    print(f"Events read: {len(records)} ({len(chained)} chained in {len(chains)} chain(s), {legacy} written before chaining)")
    unkeyed = unkeyed_chains(chained)
    if unkeyed:
        print(f"Note: {len(unkeyed)} chain(s) were written without AUDIT_HMAC_KEY: changes are detected, deliberate forgery is not.")
    if problems:
        print(f"TAMPERING OR LOSS DETECTED: {len(problems)} problem(s)")
        for p in problems:
            print("  -", p)
        return 1
    print("Audit trail intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())