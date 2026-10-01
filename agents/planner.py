"""Planner agent: input guardrail, mask, retrieve approved content through the
gateway, ask the model, output guardrail, restore details, publish."""

import json
import time

import requests
from confluent_kafka import Consumer, Producer

from agents.prompts import SYSTEM_PROMPT
from gateway.gateway import ToolCallDenied, call_tool
from guardrails.rules import check_input, check_output
from privacy.masking import mask, unmask

AGENT_NAME = "planner"
BOOTSTRAP = "localhost:9092"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "mistral"

consumer = Consumer({
    "bootstrap.servers": BOOTSTRAP,
    "group.id": "planner-agent",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
})
producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
consumer.subscribe(["chat.requests"])
print("Planner agent listening on chat.requests...")


def audit_guardrail(request_id: str, stage: str, decision) -> None:
    producer.produce("audit.guardrails", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": AGENT_NAME,
        "stage": stage,                      # "input" or "output"
        "allowed": decision.allowed,
        "category": decision.category,
        "reason": decision.reason,
        "timestamp": time.time(),
    }))


def publish_response(request_id: str, answer: str, sources: list, guardrail: dict | None) -> None:
    producer.produce("chat.responses", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": AGENT_NAME,
        "answer": answer,
        "sources": sources,
        "guardrail": guardrail,
    }))
    producer.flush()


while True:
    msg = consumer.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        print("Kafka error:", msg.error())
        continue

    req = json.loads(msg.value())
    request_id = req["request_id"]

    # 1. Input guardrail on the ORIGINAL text. It runs locally, so nothing leaves
    #    the system; unsafe requests never reach masking, tools, or the model.
    in_check = check_input(req["message"])
    audit_guardrail(request_id, "input", in_check)
    if not in_check.allowed:
        publish_response(request_id, in_check.response, [],
                         {"stage": "input", "category": in_check.category})
        consumer.commit(message=msg)
        print(f"Blocked at input ({in_check.category}): {request_id}")
        continue

    # 2. Mask personal details before anything goes to tools or the model.
    masked_message, mapping = mask(req["message"])

    # 3. Retrieve approved content through the gateway (policy-checked and audited).
    try:
        kb = call_tool(AGENT_NAME, "search_knowledge", {"query": masked_message, "limit": 3}, request_id)
        notes = kb.get("results", [])
    except ToolCallDenied as e:
        print("Denied:", e)
        notes = []
    notes_text = "\n".join(f"- {n['title']}: {n['text']}" for n in notes) or "- (no approved notes found)"

    # 4. Audit exactly what the model saw.
    producer.produce("audit.model_inputs", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": AGENT_NAME,
        "model": MODEL,
        "model_input": masked_message,
        "notes_used": [n["id"] for n in notes],
        "entities_masked": len(mapping),
    }))

    # 5. Ask the model.
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nApproved notes:\n" + notes_text},
            {"role": "user", "content": masked_message},
        ],
    }, timeout=120)
    resp.raise_for_status()
    model_answer = resp.json()["choices"][0]["message"]["content"]

    # 6. Output guardrail on the RESTORED answer, so masked names can't hide anything.
    restored_answer = unmask(model_answer, mapping)
    out_check = check_output(restored_answer)
    audit_guardrail(request_id, "output", out_check)
    if not out_check.allowed:
        publish_response(request_id, out_check.response, [],
                         {"stage": "output", "category": out_check.category})
        consumer.commit(message=msg)
        print(f"Blocked at output ({out_check.category}): {request_id}")
        continue

    # 7. Publish the answer with its sources, then commit.
    sources = [{"id": n["id"], "title": n["title"]} for n in notes]
    publish_response(request_id, restored_answer, sources, None)
    consumer.commit(message=msg)
    print(f"Answered {request_id} (masked {len(mapping)}, notes {[n['id'] for n in notes]})")