import json
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from confluent_kafka import Consumer, Producer
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agents import actions
from api.events import buffer, consume_events, event_stream
from audit.chain import key_from_env, unkeyed_chains, verify_chain
from profiles.loader import load_profile

BOOTSTRAP = "localhost:9092"
UI_DIR = Path(__file__).resolve().parent.parent / "ui"

producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
PROFILE = load_profile()        # same PROFILE as the agents (set by scripts/dev.sh)
responses: dict[str, dict] = {}  # in-memory for now; replaced later
approvals = actions.ApprovalState()   # rebuilt from Kafka at startup


def consume_responses():
    consumer = Consumer({
        "bootstrap.servers": BOOTSTRAP,
        # A new group on every start, reading from the beginning without committing,
        # so the in-memory store is rebuilt after a restart.
        "group.id": f"api-responses-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe(["chat.responses"])
    while True:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        data = json.loads(msg.value())
        responses[data["request_id"]] = data


def consume_approvals():
    consumer = Consumer({
        "bootstrap.servers": BOOTSTRAP,
        "group.id": f"api-approvals-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe(actions.APPROVAL_TOPICS)
    while True:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        approvals.apply(msg.topic(), json.loads(msg.value()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=consume_responses, daemon=True).start()
    threading.Thread(target=consume_approvals, daemon=True).start()
    threading.Thread(target=consume_events, args=(BOOTSTRAP,), daemon=True).start()
    yield
    producer.flush()


app = FastAPI(title="Governed Agentic AI - Chat API", lifespan=lifespan)


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    user_id: str                # demo only: a real deployment takes the approver from sign-in
    role: str | None = None
    note: str | None = None


class ChatRequest(BaseModel):
    user_id: str
    message: str
    role: str | None = None     # demo only: a real deployment takes the role from sign-in


@app.get("/", include_in_schema=False)
def chat_ui():
    """Simple chat window for local testing."""
    return FileResponse(UI_DIR / "index.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Live view of the pipeline: every request moving through the controls."""
    return FileResponse(UI_DIR / "dashboard.html")


@app.get("/events", include_in_schema=False)
async def events(request: Request):
    last_id = int(request.headers.get("last-event-id", 0) or 0)
    return StreamingResponse(event_stream(last_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/audit/verify")
def audit_verify():
    """Check the tamper-evident audit chain over every audit event seen since startup."""
    records = list(buffer.audit_records)
    problems = verify_chain(records, key_from_env())
    return {
        "events": len(records),
        "chains": len({r["audit"]["chain_id"] for r in records}),
        "intact": not problems,
        "unkeyed_chains": len(unkeyed_chains(records)),
        "problems": problems[:20],
    }


@app.get("/profile")
def profile_info():
    """What the chat page needs: the active profile, its roles and example questions."""
    return {
        "name": PROFILE.name,
        "title": PROFILE.title,
        "roles": [{"key": k, "title": r.title} for k, r in PROFILE.roles.items()],
        "examples": list(PROFILE.examples),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def send_chat(req: ChatRequest):
    request_id = str(uuid.uuid4())
    event = {"request_id": request_id, "user_id": req.user_id, "message": req.message, "role": req.role}
    producer.produce("chat.requests", key=request_id, value=json.dumps(event))
    producer.flush()
    return {"request_id": request_id}


@app.get("/chat/{request_id}")
def get_chat(request_id: str):
    if request_id not in responses:
        return {"status": "pending"}
    return {"status": "done", **responses[request_id]}


@app.get("/approvals")
def list_approvals():
    """Proposed changes, newest first. IDs and masked text only."""
    return sorted(approvals.approvals.values(), key=lambda a: a["created_at"], reverse=True)[:50]


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str):
    a = approvals.approvals.get(approval_id)
    if a is None:
        raise HTTPException(404, "no such approval request")
    return a


@app.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, d: Decision):
    """Record a person's decision. Whether the change is then made is decided by the
    executor agent and OPA (approver role, not the person who asked, not expired),
    not here."""
    a = approvals.approvals.get(approval_id)
    if a is None:
        raise HTTPException(404, "no such approval request")
    if a["status"] != "pending":
        raise HTTPException(409, f"already {a['status']}")
    producer.produce(actions.DECIDED, key=approval_id, value=json.dumps({
        "approval_id": approval_id, "request_id": a["request_id"], "decision": d.decision, "approver": d.user_id,
        "approver_role": d.role, "note": d.note, "decided_at": datetime.now().isoformat(timespec="seconds"),
    }))
    producer.flush()
    return {"approval_id": approval_id, "decision": d.decision, "status": "recorded"}