import json
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from confluent_kafka import Consumer, Producer
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOOTSTRAP = "localhost:9092"
UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "index.html"

producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
responses: dict[str, dict] = {}  # in-memory for now; replaced later


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=consume_responses, daemon=True).start()
    yield
    producer.flush()


app = FastAPI(title="Governed Agentic AI - Chat API", lifespan=lifespan)


class ChatRequest(BaseModel):
    user_id: str
    message: str


@app.get("/", include_in_schema=False)
def chat_ui():
    """Simple chat window for local testing."""
    return FileResponse(UI_FILE)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def send_chat(req: ChatRequest):
    request_id = str(uuid.uuid4())
    event = {"request_id": request_id, "user_id": req.user_id, "message": req.message}
    producer.produce("chat.requests", key=request_id, value=json.dumps(event))
    producer.flush()
    return {"request_id": request_id}


@app.get("/chat/{request_id}")
def get_chat(request_id: str):
    if request_id not in responses:
        return {"status": "pending"}
    return {"status": "done", **responses[request_id]}