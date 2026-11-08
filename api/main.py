import asyncio
import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from confluent_kafka import Consumer, Producer
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agents import actions
from api import openai_compat as compat
from api.events import buffer, consume_events, event_stream
from audit.chain import chains_of_profile, key_from_env, unkeyed_chains, verify_chain
from auth.sign_in import Caller, SignIn, SignInRequired
from profiles.loader import load_profile

BOOTSTRAP = "127.0.0.1:9092"
UI_DIR = Path(__file__).resolve().parent.parent / "ui"

producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
PROFILE = load_profile()        # same PROFILE as the agents (set by scripts/dev.sh)
responses: dict[str, dict] = {}  # in-memory for now; replaced later
approvals = actions.ApprovalState()   # rebuilt from Kafka at startup
# OpenAI-compatible endpoint (ADR 020): Bearer keys, comma-separated. Empty: open (local development only).
COMPAT_KEYS = [k.strip() for k in os.environ.get("OPENAI_COMPAT_API_KEYS", "").split(",") if k.strip()]
COMPAT_TIMEOUT = float(os.environ.get("OPENAI_COMPAT_TIMEOUT", "180"))
log = logging.getLogger("api")
# Who is asking (ADR 028): from the identity provider's token when sign-in is oidc, from the request
# body only in demo mode. owners: which signed-in person asked each request (only they may read it).
SIGN_IN = SignIn.for_profile(PROFILE)
owners: dict[str, str] = {}


def who(request: Request, demo_user: str | None = None, demo_role: str | None = None) -> Caller:
    """The caller of this request. In oidc mode the body's user and role are ignored."""
    try:
        return SIGN_IN.caller(request.headers.get("authorization"), demo_user, demo_role)
    except SignInRequired as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": 'Bearer realm="governed-api"'}) from None


def signed_in(request: Request) -> Caller | None:
    """In oidc mode: the signed-in caller (or 401). In demo mode: None (these routes are open)."""
    return who(request) if SIGN_IN.on else None


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
        try:
            data = json.loads(msg.value())
            responses[data["request_id"]] = data
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
            # One bad event must not end this thread: the API would answer "pending" forever.
            print(f"Skipped a malformed event on chat.responses ({type(e).__name__})", flush=True)


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
        approvals.apply_raw(msg.topic(), msg.value())


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=consume_responses, daemon=True).start()
    threading.Thread(target=consume_approvals, daemon=True).start()
    threading.Thread(target=consume_events, args=(BOOTSTRAP,), daemon=True).start()
    if SIGN_IN.on:
        log.warning("Sign-in: oidc, issuer %s. Requests without a valid access token are refused.",
                    SIGN_IN.verifier.issuer)
    else:
        log.warning("Sign-in: demo. The request says who is asking and in which role (one machine only).")
    if not COMPAT_KEYS and not SIGN_IN.on:
        log.warning("OPENAI_COMPAT_API_KEYS is not set: /v1/chat/completions accepts any caller "
                    "(local development only).")
    yield
    producer.flush()


app = FastAPI(title="Governed Agentic AI - Chat API", lifespan=lifespan)


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    user_id: str = Field(default="demo-reviewer", max_length=200)   # demo mode only; oidc: from the token
    role: str | None = None                                          # demo mode only; oidc: from the token
    note: str | None = None


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    user_id: str = Field(default="demo-user", max_length=200)   # demo mode only; oidc: from the token
    message: str = Field(min_length=1, max_length=4000)   # same limit as a history turn; bounds work per request
    role: str | None = None     # demo mode only; oidc: the role comes from the person's groups
    history: list[Turn] = Field(default_factory=list, max_length=40)   # earlier turns, oldest first (ADR 019)
    web: bool | None = None     # the chat page's "Search the web" switch; None: the wording decides


@app.get("/", include_in_schema=False)
def front_page():
    """The demo view: the chat and, beside it, each safety step in plain words. The front door."""
    return FileResponse(UI_DIR / "demo.html")


@app.get("/chat", include_in_schema=False)
def chat_ui():
    """Plain chat window for local testing (badges only, no explanation)."""
    return FileResponse(UI_DIR / "index.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Live view of the pipeline: every request moving through the controls."""
    return FileResponse(UI_DIR / "dashboard.html")


@app.get("/demo", include_in_schema=False)
def demo():
    """Kept for old links: the demo view is now the front page."""
    return FileResponse(UI_DIR / "demo.html")


@app.get("/auth.js", include_in_schema=False)
def auth_js():
    """Sign-in for the browser pages (authorization code with PKCE; no secret in the page)."""
    return FileResponse(UI_DIR / "auth.js", media_type="text/javascript")


@app.get("/auth/config")
def auth_config():
    """Where the pages send people to sign in (public settings only)."""
    return SIGN_IN.public_config()


@app.get("/auth/me")
def auth_me(request: Request):
    """Who the token belongs to and the role it gives here (oidc), or demo mode."""
    if not SIGN_IN.on:
        return {"mode": "demo"}
    c = who(request)
    return {"mode": "oidc", "user": c.user_id, "name": c.name, "role": c.role,
            "role_title": PROFILE.roles[c.role].title if c.role in PROFILE.roles else None,
            "may_watch": SIGN_IN.may_watch(c)}


@app.get("/events", include_in_schema=False)
async def events(request: Request):
    c = await asyncio.to_thread(signed_in, request)     # a key fetch must not block the event loop
    if c is not None and not SIGN_IN.may_watch(c):
        raise HTTPException(403, "your role may not watch the live dashboard")
    last_id = int(request.headers.get("last-event-id", 0) or 0)
    return StreamingResponse(event_stream(last_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/audit/verify")
def audit_verify(scope: Literal["profile", "all"] = "profile"):
    """Check the tamper-evident audit chain over the audit events seen since startup: the chains
    that wrote for the active profile (default), or every chain in Kafka (scope=all)."""
    records = list(buffer.audit_records)
    if scope == "profile":
        records = chains_of_profile(records, PROFILE.name)
    problems = verify_chain(records, key_from_env())
    return {
        "scope": "all profiles" if scope == "all" else PROFILE.name,
        "events": len(records),
        "chains": len({r["audit"]["chain_id"] for r in records}),
        "intact": not problems,
        "unkeyed_chains": len(unkeyed_chains(records)),
        "problems": problems[:20],
    }


@app.get("/evaluations")
def evaluations():
    """The latest offline tool-use accuracy result (eval/run_tool_use.py), for the dashboard. Marked
    out of date when the routing patterns or code have changed since it was scored."""
    from eval import run_tool_use as tu
    runs = sorted(tu.RESULTS.glob("tool_use_v1_*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        return {"tool_use": None}
    d = json.loads(runs[-1].read_text())
    profiles = sorted({r["profile"] for r in d["rows"]})
    return {"tool_use": {"set": d["set"], "config": d["config"], "run_at": d["run_at"],
                         "both": d["summary"]["all"]["both"], "routing": d["summary"]["all"]["routing"],
                         "missed": d["summary"]["missed"], "false_trigger": d["summary"]["false_trigger"],
                         "current": d["config"] == tu.config_id(profiles)}}


@app.get("/profile")
def profile_info():
    """What the chat page needs: the active profile, its roles and example questions."""
    return {
        "name": PROFILE.name,
        "title": PROFILE.title,
        "roles": [{"key": k, "title": r.title} for k, r in PROFILE.roles.items()],
        "examples": list(PROFILE.examples),
        "web": any(t.data_label == "web_page" for t in PROFILE.tools.values()),   # show the web switch
        # Off for a profile served only through the governed endpoint (no pipeline chat running).
        "chat": os.environ.get("DASHBOARD_CHAT", "on") != "off",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def submit(user_id: str, message: str, role: str | None, history: list[dict], web: bool | None = None) -> str:
    """Put a message on the pipeline. Every entry point (/chat, /v1/chat/completions) goes through here."""
    request_id = str(uuid.uuid4())
    event = {"request_id": request_id, "user_id": user_id, "message": message, "role": role,
             "profile": PROFILE.name, "history": history}
    if web is not None:
        event["web"] = web
    producer.produce("chat.requests", key=request_id, value=json.dumps(event))
    producer.flush()
    return request_id


@app.post("/chat")
def send_chat(req: ChatRequest, request: Request):
    c = who(request, req.user_id, req.role)
    request_id = submit(c.user_id, req.message, c.role, [t.model_dump() for t in req.history], req.web)
    if SIGN_IN.on:
        owners[request_id] = c.user_id
    return {"request_id": request_id}


@app.get("/v1/models")
def compat_models(request: Request):
    if SIGN_IN.on:
        who(request)
    elif not compat.authorized(request.headers.get("authorization"), COMPAT_KEYS):
        return JSONResponse(compat.CompatError("Invalid API key.", 401, "invalid_api_key").body(), 401)
    return compat.models(PROFILE.name)


@app.post("/v1/chat/completions")
async def compat_chat(request: Request):
    """OpenAI-compatible chat (ADR 020): the same pipeline as /chat, in the OpenAI request and
    response shapes, so an existing chatbot can be put behind the governance layer by its base URL.
    With sign-in on (oidc) the bearer is the person's access token instead of an API key."""
    if SIGN_IN.on:
        try:
            caller = await asyncio.to_thread(SIGN_IN.caller, request.headers.get("authorization"))
        except SignInRequired as e:
            return JSONResponse(compat.CompatError(str(e), 401, "invalid_api_key").body(), 401)
    elif not compat.authorized(request.headers.get("authorization"), COMPAT_KEYS):
        return JSONResponse(compat.CompatError("Invalid API key.", 401, "invalid_api_key").body(), 401)
    else:
        caller = None
    try:
        req = compat.to_request(await request.json())
    except ValueError:
        return JSONResponse(compat.CompatError("The request body is not valid JSON.").body(), 400)
    except compat.CompatError as e:
        return JSONResponse(e.body(), e.status)
    if caller is not None:
        user, role = caller.user_id, caller.role
    else:   # demo mode, like /chat: the caller names the role
        user, role = req["user_id"], request.headers.get("x-governed-role") or None
    request_id = submit(user, req["message"], role, req["history"])
    waited = 0.0
    while request_id not in responses:
        if waited >= COMPAT_TIMEOUT:
            err = compat.CompatError(f"No answer within {COMPAT_TIMEOUT:.0f} seconds (request {request_id}). "
                                     "Is the pipeline running?", 504, "timeout")
            return JSONResponse(err.body(), 504)
        await asyncio.sleep(0.2)
        waited += 0.2
    completion = compat.to_completion(responses[request_id], PROFILE.name, req["dropped_system_messages"])
    if req["stream"]:
        return StreamingResponse(compat.stream_events(completion), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return completion


@app.get("/chat/{request_id}")
def get_chat(request_id: str, request: Request):
    c = signed_in(request)
    if c is not None and owners.get(request_id) != c.user_id:
        raise HTTPException(404, "no such request")     # only the person who asked may read the answer
    if request_id not in responses:
        return {"status": "pending"}
    return {"status": "done", **responses[request_id]}


@app.get("/approvals")
def list_approvals(request: Request):
    """Proposed changes, newest first. IDs and masked text only."""
    signed_in(request)
    return sorted(approvals.approvals.values(), key=lambda a: a["created_at"], reverse=True)[:50]


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str, request: Request):
    signed_in(request)
    a = approvals.approvals.get(approval_id)
    if a is None:
        raise HTTPException(404, "no such approval request")
    return a


@app.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, d: Decision, request: Request):
    """Record a person's decision. Whether the change is then made is decided by the
    executor agent and OPA (approver role, not the person who asked, not expired),
    not here. With sign-in on, the approver and their role come from their token, so
    "not the person who asked" compares two signed-in people."""
    c = who(request, d.user_id, d.role)
    a = approvals.approvals.get(approval_id)
    if a is None:
        raise HTTPException(404, "no such approval request")
    if a["status"] != "pending":
        raise HTTPException(409, f"already {a['status']}")
    producer.produce(actions.DECIDED, key=approval_id, value=json.dumps({
        "approval_id": approval_id, "request_id": a["request_id"], "decision": d.decision, "approver": c.user_id,
        "approver_role": c.role, "note": d.note, "decided_at": datetime.now().isoformat(timespec="seconds"),
    }))
    producer.flush()
    return {"approval_id": approval_id, "decision": d.decision, "status": "recorded"}
