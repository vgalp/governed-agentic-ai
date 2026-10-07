"""Planner agent: input guardrails, mask, retrieve approved content through the
gateway, ask the model, output guardrails, restore details, publish.

Guardrails run in two layers: fast rules (layer 1), then a safety classifier
(layer 2) for what the rules miss.

Everything specific to the use case (model, prompt, rules, replies, classifier
policy, privacy settings, knowledge base, tool permissions) comes from the
active profile: PROFILE=<name>, default adhd-assistant. See profiles/."""

import json

import requests
from confluent_kafka import Consumer, Producer

from audit.chain import get_audit_chain
from gateway.gateway import ToolCallDenied, call_tool
from guardrails.classifier import classifier_check
from guardrails.rules import check_input, check_output
from llm.providers import ModelUnavailable, chat
from privacy.masking import mask, unmask
from profiles.loader import load_profile
from router.router import RouteDecision, route

AGENT_NAME = "planner"
BOOTSTRAP = "localhost:9092"

PROFILE = load_profile()          # fails here, at startup, if the profile is incomplete

consumer = Consumer({
    "bootstrap.servers": BOOTSTRAP,
    "group.id": "planner-agent",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
})
producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})
consumer.subscribe(["chat.requests"])
audit = get_audit_chain(AGENT_NAME)   # tamper-evident audit, shared with the gateway
_ext = PROFILE.external_model["name"] if PROFILE.routing["external_allowed"] else "none"
print(f"Planner agent listening on chat.requests (profile {PROFILE.name}, "
      f"local model {PROFILE.local_model['name']}, external model {_ext}, "
      f"guard {PROFILE.classifier.model})...")


def audit_guardrail(request_id: str, stage: str, layer: str, decision) -> None:
    audit.emit("audit.guardrails", {
        "request_id": request_id,
        "profile": PROFILE.name,
        "agent": AGENT_NAME,
        "stage": stage,                      # "input" or "output"
        "layer": layer,                      # "rules" or "classifier"
        "allowed": decision.allowed,
        "category": decision.category,
        "reason": decision.reason,
    })


def publish_response(request_id: str, answer: str, sources: list, guardrail: dict | None,
                     route_info: dict | None = None) -> None:
    producer.produce("chat.responses", key=request_id, value=json.dumps({
        "request_id": request_id,
        "profile": PROFILE.name,
        "agent": AGENT_NAME,
        "answer": answer,
        "sources": sources,
        "guardrail": guardrail,
        "route": route_info,               # which model answered: shown as a badge in the UI
    }))
    producer.flush()
    audit.producer.flush()


while True:
    msg = consumer.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        print("Kafka error:", msg.error())
        continue

    req = json.loads(msg.value())
    request_id = req["request_id"]

    # 1. Input guardrails on the ORIGINAL text. Both layers run locally, so nothing
    #    leaves the system; unsafe requests never reach masking, tools, or the model.
    in_check = check_input(req["message"], PROFILE)
    in_layer = "rules"
    if in_check.allowed:
        in_check = classifier_check(req["message"], profile=PROFILE)
        in_layer = "classifier"
    audit_guardrail(request_id, "input", in_layer, in_check)
    if not in_check.allowed:
        publish_response(request_id, in_check.response, [],
                         {"stage": "input", "layer": in_layer, "category": in_check.category})
        consumer.commit(message=msg)
        print(f"Blocked at input by {in_layer} ({in_check.category}): {request_id}")
        continue

    # 2. Mask personal details before anything goes to tools or the model.
    masked_message, mapping = mask(req["message"], PROFILE)

    # 3. Retrieve approved content through the gateway (policy-checked and audited).
    try:
        kb = call_tool(AGENT_NAME, "search_knowledge", {"query": masked_message, "limit": 3},
                       request_id, PROFILE)
        notes = kb.get("results", [])
    except ToolCallDenied as e:
        print("Denied:", e)
        notes = []
    notes_text = "\n".join(f"- {n['title']}: {n['text']}" for n in notes) or "- (no approved notes found)"

    # 4. Decide which model answers (OPA policy, audited). External models only
    #    ever get masked text; the local model gets what the profile says.
    decision = route(PROFILE, mapping, ["search_knowledge"], request_id, AGENT_NAME)

    # 5. Ask the model. If an external model fails, fall back to the local one.
    def ask(d: RouteDecision) -> str:
        user_text = masked_message if d.send_masked else req["message"]
        # Audit what the model saw. The audit never holds raw personal data:
        # when the local model sees the original, the masked text is recorded.
        audit.emit("audit.model_inputs", {
            "request_id": request_id,
            "profile": PROFILE.name,
            "agent": AGENT_NAME,
            "target": d.target,
            "model": d.model["name"],
            "model_input": masked_message,
            "sent_masked": d.send_masked,
            "notes_used": [n["id"] for n in notes],
            "entities_masked": len(mapping),
        })
        return chat(d.model, [
            {"role": "system", "content": PROFILE.system_prompt + "\n\nApproved notes:\n" + notes_text},
            {"role": "user", "content": user_text},
        ])

    try:
        model_answer = ask(decision)
    except (requests.RequestException, ModelUnavailable, KeyError) as e:
        if decision.target != "external":
            raise
        print(f"External model failed ({type(e).__name__}); using the local model: {request_id}")
        decision = RouteDecision("local", PROFILE.routing["local_model"], PROFILE.local_model,
                                 send_masked=PROFILE.routing["local_input"] == "masked",
                                 reasons=["external model failed: fell back to the local model"])
        audit.emit("audit.routing", {
            "request_id": request_id, "profile": PROFILE.name, "agent": AGENT_NAME,
            "target": "local", "model_key": decision.model_key, "model": decision.model["name"],
            "provider": decision.model["provider"], "sent_masked": decision.send_masked,
            "fallback": True, "reasons": decision.reasons,
        })
        model_answer = ask(decision)
    route_info = {"target": decision.target, "model": decision.model["name"]}

    # 6. Output guardrails on the RESTORED answer, so masked names can't hide anything.
    #    The classifier sees the question and the answer together.
    restored_answer = unmask(model_answer, mapping) if decision.send_masked else model_answer
    out_check = check_output(restored_answer, PROFILE)
    out_layer = "rules"
    if out_check.allowed:
        out_check = classifier_check(req["message"], restored_answer, PROFILE)
        out_layer = "classifier"
    audit_guardrail(request_id, "output", out_layer, out_check)
    if not out_check.allowed:
        publish_response(request_id, out_check.response, [],
                         {"stage": "output", "layer": out_layer, "category": out_check.category},
                         route_info)
        consumer.commit(message=msg)
        print(f"Blocked at output by {out_layer} ({out_check.category}): {request_id}")
        continue

    # 7. Publish the answer with its sources, then commit.
    sources = [{"id": n["id"], "title": n["title"]} for n in notes]
    publish_response(request_id, restored_answer, sources, None, route_info)
    consumer.commit(message=msg)
    print(f"Answered {request_id} by {decision.target} model {decision.model['name']} "
          f"(masked {len(mapping)}, notes {[n['id'] for n in notes]})")