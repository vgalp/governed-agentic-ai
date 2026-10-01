"""Planner agent: reads chat requests, masks personal details, asks the model,
restores the details in the answer, and publishes the response."""

import json

import requests
from confluent_kafka import Consumer, Producer

from privacy.masking import mask, unmask

BOOTSTRAP = "localhost:9092"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"  # OpenAI-compatible API
MODEL = "mistral"

SYSTEM_PROMPT = (
    "You are a supportive assistant that helps adults with ADHD plan their day "
    "and build routines. "
    "Keep answers short: at most 5 bullet points, then one clear next step. "
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

    # 1. Mask personal details. The original text never goes to the model.
    masked_message, mapping = mask(req["message"])

    # 2. Audit: record exactly what the model saw (masked text only).
    producer.produce("audit.model_inputs", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": "planner",
        "model": MODEL,
        "model_input": masked_message,
        "entities_masked": len(mapping),
    }))

    # 3. Ask the model, using only the masked text.
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": masked_message},
        ],
    }, timeout=120)
    resp.raise_for_status()
    model_answer = resp.json()["choices"][0]["message"]["content"]

    # 4. Restore the real details for the user.
    answer = unmask(model_answer, mapping)

    # 5. Publish the answer, then commit the offset (at-least-once delivery).
    producer.produce("chat.responses", key=request_id, value=json.dumps({
        "request_id": request_id,
        "agent": "planner",
        "answer": answer,
    }))
    producer.flush()
    consumer.commit(message=msg)
    print(f"Answered {request_id} (masked {len(mapping)} item(s))")