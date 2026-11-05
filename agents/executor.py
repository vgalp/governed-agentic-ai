"""Executor agent: makes approved changes, and nothing else.

It reads approvals.requested and approvals.decided (Kafka). When a person decides:
  - rejected or expired: nothing is changed;
  - approved: OPA checks again (policies/actions.rego: an approver role, not the person
    who asked, not expired), and only if allowed the change is made through the gateway,
    which checks that this agent and the approver's role may use the tool.
The result goes to actions.executed. Every step is audited (audit.approvals) and traced
on the original request. Pending approvals that are not decided in time expire.

The executor never talks to a language model and never reads the request text: it acts
only on the validated arguments in the approval request.

On start it first reads every approval event already in Kafka without acting on them,
to rebuild its view of what is pending. Approvals that were approved but have no result (the
executor was down, or stopped, between the decision and the change) are then finished: the
change tool refuses to make the same change twice (one change per approval ID), so finishing one
that was in fact made only records its result. After that it acts on new decisions.

Nothing runs at import: main() connects to Kafka. Tests set PROFILE, producer and audit and call
the functions directly.
"""

import json
import time
import uuid
from datetime import datetime

import requests
from confluent_kafka import Consumer, Producer, TopicPartition

from agents import actions
from agents.trace import Trace
from audit.chain import get_audit_chain
from gateway.gateway import ToolCallDenied, call_tool
from gateway.opa import decide
from profiles.loader import load_profile

AGENT_NAME = "executor"
BOOTSTRAP = "localhost:9092"

# Set by main() (or by a test).
PROFILE = None
producer = None
audit = None
state = actions.ApprovalState()


def trace_for(approval: dict, role: str | None) -> Trace:
    return Trace(producer, PROFILE.name, approval["request_id"], role, process=AGENT_NAME,
                 started_at=approval.get("trace_started_at"))


def audit_approval(approval: dict, stage: str, **fields) -> None:
    audit.emit("audit.approvals", {"request_id": approval["request_id"], "profile": PROFILE.name,
                                   "agent": AGENT_NAME, "approval_id": approval["approval_id"],
                                   "action": approval["action"], "stage": stage, **fields})


def publish_result(approval: dict, status: str, decision: dict, result: dict | None = None,
                   reasons: list | None = None) -> None:
    producer.produce(actions.EXECUTED, key=approval["approval_id"], value=json.dumps({
        "approval_id": approval["approval_id"], "request_id": approval["request_id"],
        "status": status, "result": result, "reasons": reasons,
        "approver": decision.get("approver"), "approver_role": decision.get("approver_role"),
        "at": datetime.now().isoformat(timespec="seconds"),
    }))
    producer.flush()
    audit.producer.flush()


def execute_policy(approval: dict, decision: dict, expired: bool) -> dict:
    try:
        return decide(PROFILE, "actions/execute", {
            "profile": PROFILE.name, "action": approval["action"], "decision": decision["decision"],
            "requester": approval.get("requester"), "approver": decision.get("approver"),
            "approver_role": decision.get("approver_role"), "expired": expired,
        }) or {}
    except requests.RequestException:
        return {"allowed": False, "reasons": ["the approval policy is unreachable"]}


def on_decision(approval: dict, decision: dict) -> None:
    """A person (or the clock) decided. Make the change only if the policy allows it."""
    role = decision.get("approver_role")
    trace = trace_for(approval, role)
    who = f"{decision.get('approver') or 'system'} ({role or 'no role'})"
    if decision["decision"] != "approve":
        word = "Expired: no one approved it in time" if decision["decision"] == "expired" else f"Rejected by {who}"
        trace.step("action", "approval", "block", f"{word}; nothing was changed")
        audit_approval(approval, decision["decision"], approver=decision.get("approver"), approver_role=role)
        return

    trace.step("action", "approval", "info", f"Approval submitted by {who}")
    audit_approval(approval, "approval_submitted", approver=decision.get("approver"), approver_role=role)
    expired = datetime.fromisoformat(approval["expires_at"]) <= datetime.fromisoformat(decision["decided_at"])
    check = execute_policy(approval, decision, expired)
    if not check.get("allowed"):
        reasons = check.get("reasons") or ["denied by policy"]
        if expired:
            trace.step("action", "execute_policy", "block", f"Not made: {'; '.join(reasons)}")
            audit_approval(approval, "refused", reasons=reasons)
            publish_result(approval, "refused", decision, reasons=reasons)
            return
        # This person may not approve it. The request stays open for someone who may.
        trace.step("action", "execute_policy", "block",
                   f"Approval not accepted: {'; '.join(reasons)}. Still waiting for an approver")
        audit_approval(approval, "approval_refused", approver=decision.get("approver"),
                       approver_role=role, reasons=reasons)
        approval.update(status="pending", decided_by=None, decided_role=None)
        publish_result(approval, "approval_refused", decision, reasons=reasons)
        return
    trace.step("action", "execute_policy", "pass", "Policy allows the change: approved by an approver role")

    act = PROFILE.actions[approval["action"]]
    trace.start("action", "execute", f"{act.tool} through the gateway")
    try:
        # Only the approved values and the approval ID: what they change is set by the
        # profile, on the change server (actions.yaml, change:).
        result = call_tool(AGENT_NAME, act.tool, {"fields": approval["args"], "approval_id": approval["approval_id"]},
                           approval["request_id"], PROFILE, role=role)
    except ToolCallDenied as e:
        trace.step("action", "execute", "block", "Denied by the gateway policy")
        audit_approval(approval, "refused", reasons=[str(e)])
        publish_result(approval, "refused", decision, reasons=["denied by the gateway policy"])
        return
    except Exception as e:                    # the tool is down: nothing was changed
        trace.step("action", "execute", "error", f"The change could not be made ({type(e).__name__})")
        audit_approval(approval, "failed", reasons=[type(e).__name__])
        publish_result(approval, "failed", decision, reasons=[type(e).__name__])
        return
    status = "done" if result.get("status") in {"done", "already_done"} else "failed"
    detail = result.get("summary", "") if status == "done" else result.get("reason", "failed")
    trace.step("action", "execute", "pass" if status == "done" else "error",
               ("Done: " if status == "done" else "Failed: ") + detail)
    audit_approval(approval, status, result=result)
    publish_result(approval, status, decision, result=result,
                   reasons=None if status == "done" else [result.get("reason", "failed")])


def handle_decision(approval: dict, decision: dict) -> None:
    """on_decision, guarded: an unexpected error (a renamed action, a malformed date) is recorded
    as a failed result instead of stopping the executor, which would leave the approval approved
    and never carried out."""
    try:
        on_decision(approval, decision)
    except Exception as e:
        print(f"Executor error on {approval.get('approval_id')} ({type(e).__name__})", flush=True)
        try:
            audit_approval(approval, "failed", reasons=[f"executor error ({type(e).__name__})"])
            publish_result(approval, "failed", decision, reasons=[f"executor error ({type(e).__name__})"])
        except Exception as e2:              # Kafka itself is failing: nothing more to do here
            print(f"Could not record the failure ({type(e2).__name__})", flush=True)


def finish_interrupted() -> int:
    """After catch-up: carry out approvals that were approved but have no result. Safe to repeat:
    the change tool does each approval ID once and answers "already_done" for a repeat."""
    todo = state.approved_not_executed()
    for a in todo:
        decision = {"decision": "approve", "approver": a.get("decided_by"), "approver_role": a.get("decided_role"),
                    "decided_at": a.get("decided_at"), "note": a.get("note")}
        handle_decision(a, decision)
        a["status"] = "executing"            # the result event comes back around and sets the final status
    return len(todo)


def expire_overdue() -> None:
    """Approvals not decided in time expire. The decision is published like any other."""
    for a in state.overdue(datetime.now()):
        producer.produce(actions.DECIDED, key=a["approval_id"], value=json.dumps({
            "approval_id": a["approval_id"], "request_id": a["request_id"], "decision": "expired", "approver": None,
            "approver_role": None, "decided_at": datetime.now().isoformat(timespec="seconds"),
        }))
        a["status"] = "expiring"          # don't publish twice while the event comes back around
    producer.flush()


def catch_up(consumer: Consumer) -> None:
    """Read every approval event already in Kafka, without acting on it."""
    # Start from the oldest event Kafka still keeps (the low watermark), not offset 0: once the
    # topic's retention has deleted old events, offset 0 no longer exists, and waiting for events up
    # to the end would never finish when every event is gone.
    parts, ends, done = [], {}, set()
    for topic in actions.APPROVAL_TOPICS:
        meta = consumer.list_topics(topic, timeout=10).topics[topic]
        for p in meta.partitions:
            low, high = consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10)
            parts.append(TopicPartition(topic, p, low))
            ends[(topic, p)] = high
            if high <= low:
                done.add((topic, p))          # nothing left to read
    consumer.assign(parts)
    while len(done) < len(ends):
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        state.apply_raw(msg.topic(), msg.value())     # a malformed event is skipped, not fatal
        if msg.offset() + 1 >= ends[(msg.topic(), msg.partition())]:
            done.add((msg.topic(), msg.partition()))


def on_event(topic: str, raw) -> None:
    """One live event from Kafka."""
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        print(f"Skipped an event that is not JSON on {topic}", flush=True)   # one bad event, not a dead executor
        return
    if not isinstance(event, dict):
        return
    if topic == actions.DECIDED:
        approval = state.approvals.get(event.get("approval_id"))
        if approval is None or approval["status"] not in {"pending", "expiring"}:
            return                             # unknown, or already decided: ignore
        approval["status"] = "pending"
        state.apply_raw(topic, raw)
        handle_decision(approval, event)
    else:
        state.apply_raw(topic, raw)


def main() -> None:
    global PROFILE, producer, audit
    PROFILE = load_profile()
    producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
    audit = get_audit_chain(AGENT_NAME)
    consumer = Consumer({
        "bootstrap.servers": BOOTSTRAP,
        "group.id": f"executor-{uuid.uuid4()}",     # reads from the start every time; state comes from Kafka
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    catch_up(consumer)
    finished = finish_interrupted()
    print(f"Executor agent listening on approvals (profile {PROFILE.name}, actions {list(PROFILE.actions)}, "
          f"{len(state.pending())} pending, {finished} interrupted approval(s) finished)...", flush=True)

    last_sweep = 0.0
    while True:
        if time.time() - last_sweep > 5:
            expire_overdue()
            last_sweep = time.time()
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        on_event(msg.topic(), msg.value())


if __name__ == "__main__":
    main()
