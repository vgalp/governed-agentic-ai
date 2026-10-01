import json

import requests
from confluent_kafka import Consumer, Producer

BOOTSTRAP = "localhost:9092"
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"  # OpenAI-compatible API
SYSTEM_PROMPT = (
    "You are a supportive assistant that helps adults with ADHD plan their day "
    "and build routines. Keep answers short and practical. "
    "Never give medical, diagnostic, or medication advice."
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
    resp = requests.post(OLLAMA_URL, json={
        "model": "mistral",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": req["message"]},
        ],
    }, timeout=120)
    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"]

    out = {"request_id": req["request_id"], "agent": "planner", "answer": answer}
    producer.produce("chat.responses", key=req["request_id"], value=json.dumps(out))
    producer.flush()
    consumer.commit(message=msg)
    print("Answered", req["request_id"])