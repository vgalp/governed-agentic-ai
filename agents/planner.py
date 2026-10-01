"""Planner agent: masks personal details, retrieves approved content through the
MCP gateway, asks the model, restores details, and publishes the answer."""

import json

import requests
from confluent_kafka import Consumer, Producer

from gateway.gateway import ToolCallDenied, call_tool
from privacy.masking import mask, unmask

AGENT_NAME = "planner"
BOOTSTRAP = "localhost:9092"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "mistral"

SYSTEM_PROMPT = (
    "You are a supportive assistant that helps adults with ADHD plan their day "
    "and build routines. "
    "Keep answers short: at most 5 bullet points, then one clear next step. "
    "Base your advice on the approved notes provided; do not add other health claims. "
    "Never mention medication, diagnosis, or treatment; if asked, say the user "
    "should talk to their prescriber or clinician. "
    "Do not invent placeholders or fill-in-the-blank fields. If you need "
    "information such as a wake-up time, ask one short question instead. "
    "Text like [PERSON_1] stands for a private detail: only repeat it exactly "
    "if you must refer to it."
)

consumer = Consumer({
    "bootstrap.servers": BOOTSTRAP,
    "group.id": "planner-agent",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
})
producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
consumer.subscribe(["chat.requests"])
print("Planner agent listening on chat.requests...")

while True:
    msg = consumer.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        print("Kafka error:", msg.error())
        continue

    req = json.loads(msg.value())
    request_id = req["request_id"]

    # 1. Mask personal details.
    masked_message, mapping = mask(req["message"])

    # 2. Retrieve approved content through the gateway (policy-checked and audited).
    try:
        kb = call_tool(AGENT_NAME, "search_knowledge", {"query": masked_message, "limit": 3}, request_id)
        notes = kb.get("results", [])
    except ToolCallDenied as e:
        print("Denied:", e)
        notes = []

    notes_text = "\n".join(f"- {n['title']}: {n['text']}" for n in notes) or "- (no approved notes found)"

    # 3. Audit exactly what the model saw.
    producer.produce("audit.model_inputs", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": AGENT_NAME,
        "model": MODEL,
        "model_input": masked_message,
        "notes_used": [n["id"] for n in notes],
        "entities_masked": len(mapping),
    }))

    # 4. Ask the model with the masked message and the approved notes.
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nApproved notes:\n" + notes_text},
            {"role": "user", "content": masked_message},
        ],
    }, timeout=120)
    resp.raise_for_status()
    answer = unmask(resp.json()["choices"][0]["message"]["content"], mapping)

    # 5. Publish the answer with its sources, then commit.
    producer.produce("chat.responses", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": AGENT_NAME,
        "answer": answer,
        "sources": [{"id": n["id"], "title": n["title"]} for n in notes],
    }))
    producer.flush()
    consumer.commit(message=msg)
    print(f"Answered {request_id} (masked {len(mapping)}, notes {[n['id'] for n in notes]})")