"""Planner agent: input guardrails, mask, retrieve approved content through the
gateway, ask the model, output guardrails, restore details, publish.

Guardrails run in layers: fast rules (layer 1), then a safety classifier (layer 2)
for what the rules miss. On answers, profiles that cite sources also get a grounding
check: an answer may only cite sources the model was given.

Everything specific to the use case (model, prompt, rules, replies, classifier
policy, privacy settings, knowledge base, tool permissions, roles) comes from the
active profile: PROFILE=<name>, default adhd-assistant. See profiles/.

In a profile with roles, the requesting role decides which tools may be used and
which record data is fetched (OPA). The role is checked after the input guardrails,
so a crisis message is answered with the crisis reply even without a role.

Every step is also published to agent.trace as it happens (agents/trace.py): a readable
decision trace for the dashboard, with no personal data.

A request for a change (profiles with actions, e.g. "move this appointment") is not
answered: the planner prepares a proposal for a person to approve (agents/actions.py).
It cannot make the change; only the executor agent can, after approval."""

import json
from datetime import datetime

import requests
from confluent_kafka import Consumer, Producer

from agents import actions
from agents.trace import Trace, ids_text, masking_summary
from audit.chain import get_audit_chain
from gateway.gateway import ToolCallDenied, call_tool
from gateway.opa import decide
from guardrails.classifier import classifier_check
from guardrails.grounding import check_citations
from guardrails.rules import GuardrailDecision, check_input, check_output, classifier_exemption
from llm.providers import ModelUnavailable, chat
from privacy.masking import mask, unmask
from profiles.loader import load_profile
from router.router import RouteDecision, audit_route, fallbacks, local_decision, route

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
_ext = (" -> ".join(f"{PROFILE.models[k]['provider']}/{PROFILE.models[k]['name']}"
                    for k in [PROFILE.routing["external_model"], *PROFILE.routing["fallback"]])
        if PROFILE.routing["external_allowed"] else "none")
print(f"Planner agent listening on chat.requests (profile {PROFILE.name}, "
      f"local model {PROFILE.local_model['name']}, external model {_ext}, "
      f"guard {PROFILE.classifier.model})...")


def audit_guardrail(request_id: str, stage: str, layer: str, decision, role: str | None = None,
                    override: str | None = None) -> None:
    audit.emit("audit.guardrails", {
        "request_id": request_id,
        "profile": PROFILE.name,
        "agent": AGENT_NAME,
        "role": role,
        "stage": stage,                      # "input" or "output"
        "layer": layer,                      # "rules", "classifier" or "grounding"
        "allowed": decision.allowed,
        "category": decision.category,
        "reason": decision.reason,
        "override": override,                # set when a classifier flag was overridden by an exemption
    })


def reply(decision, role: str | None) -> str:
    """The fixed reply for a blocked request. The decision is the same for every role;
    only the wording may differ (roles.yaml). An unknown or missing role gets the default."""
    if decision.category in PROFILE.responses:
        return PROFILE.response(decision.category, role)
    return decision.response        # e.g. the classifier was unavailable


def publish_response(request_id: str, answer: str, sources: list, guardrail: dict | None,
                     route_info: dict | None = None, access: dict | None = None,
                     approval: dict | None = None) -> None:
    producer.produce("chat.responses", key=request_id, value=json.dumps({
        "request_id": request_id,
        "profile": PROFILE.name,
        "agent": AGENT_NAME,
        "answer": answer,
        "sources": sources,
        "guardrail": guardrail,
        "route": route_info,               # which model answered: shown as a badge in the UI
        "access": access,                  # role and the data classes it may not see
        "approval": approval,              # a proposed change waiting for a person's approval
    }))
    producer.flush()
    audit.producer.flush()


def audit_approval(request_id: str, role: str | None, stage: str, **fields) -> None:
    audit.emit("audit.approvals", {"request_id": request_id, "profile": PROFILE.name,
                                   "agent": AGENT_NAME, "role": role, "stage": stage, **fields})


def action_policy(action, role: str | None, facts: dict) -> dict:
    """OPA decides (policies/actions.rego). OPA unreachable: denied (fail closed)."""
    try:
        return decide(PROFILE, "actions/decision", {"profile": PROFILE.name, "action": action.name,
                                                    "role": role, "facts": facts}) or {}
    except requests.RequestException:
        return {"allowed": False, "reasons": ["the approval policy is unreachable"]}


def propose(action, req: dict, request_id: str, role: str | None, notes: list[dict], notes_text: str,
            access: dict | None, trace: Trace) -> str:
    """Turn a change request into a proposal for a person to approve. Nothing is changed.
    Returns what happened: proposed, denied or unclear."""
    def unclear(reason: str) -> str:
        trace.step("action", "proposal", "block", f"Could not prepare the change: {reason}")
        audit_approval(request_id, role, "unclear", action=action.name, reason=reason)
        publish_response(request_id, PROFILE.response("action_unclear", role).format(reason=reason),
                         [], {"stage": "action", "layer": "proposal", "category": "action_unclear"},
                         access=access)
        return "unclear"

    given = [n["id"] for n in notes]
    if not given:
        return unclear("I found no records for that request")
    # Proposals are always prepared by the local model: they are built from patient records.
    d = local_decision(PROFILE, ["proposals are always prepared by the local model"])
    audit_route(PROFILE, d, request_id, AGENT_NAME, role, fallback=False)
    now = datetime.now()
    trace.start("action", "proposal", f"Local model {d.model['name']} preparing the proposed change "
                                      "(it cannot make the change)")
    masked, mapping = mask(req["message"], PROFILE)
    audit.emit("audit.model_inputs", {
        "request_id": request_id, "profile": PROFILE.name, "agent": AGENT_NAME, "role": role,
        "target": d.target, "model": d.model["name"], "model_input": masked,
        "sent_masked": d.send_masked, "notes_used": given, "entities_masked": len(mapping),
        "purpose": f"proposal: {action.name}",
    })
    try:
        raw = chat(d.model, actions.extraction_messages(action, notes_text, req["message"], now))
    except (requests.RequestException, ModelUnavailable):
        return unclear("the local model is unavailable")
    proposal = actions.parse_proposal(raw)
    if isinstance(proposal, str):
        return unclear(proposal)
    args, error = actions.validate(action, proposal, given)
    if error:
        return unclear(error)
    trace.step("action", "proposal", "pass", "Proposed: " + ", ".join(f"{k} {v}" for k, v in args.items()),
               args=args)

    decision = action_policy(action, role, actions.facts(action, args, now))
    if not decision.get("allowed"):
        reasons = decision.get("reasons") or ["denied by policy"]
        trace.step("action", "policy", "block", f"Denied by policy: {'; '.join(reasons)}")
        audit_approval(request_id, role, "denied", action=action.name, args=args, reasons=reasons)
        publish_response(request_id, PROFILE.response("action_denied", role).format(reasons="; ".join(reasons)),
                         [], {"stage": "action", "layer": "policy", "category": "action_denied"}, access=access)
        return "denied"

    text = actions.summary(action, args, notes, masker=lambda t: mask(t, PROFILE)[0])
    approval = actions.new_approval(PROFILE, action, args, text, request_id, req.get("user_id"), role,
                                    decision.get("approve_roles", []), now)
    approvers = " or ".join(PROFILE.roles[r].title.lower() for r in approval["approve_roles"])
    trace.step("action", "policy", "pass", f"Allowed by policy; needs approval by {approvers}")
    producer.produce(actions.REQUESTED, key=approval["approval_id"],
                     value=json.dumps({**approval, "trace_started_at": trace.started_at}))
    audit_approval(request_id, role, "proposed", approval_id=approval["approval_id"], action=action.name,
                   args=args, summary=text, requester=approval["requester"],
                   approve_roles=approval["approve_roles"], expires_at=approval["expires_at"])
    trace.step("action", "approval", "running",
               f"Waiting for approval {approval['approval_id']} until {approval['expires_at'][11:16]}")
    reply_text = PROFILE.response("action_pending", role).format(
        approvers=approvers, expires=action.expires_minutes, summary=text, approval_id=approval["approval_id"])
    publish_response(request_id, reply_text, [{"id": i, "title": "record"} for i in given if i in args.values()],
                     None, {"target": d.target, "model": d.model["name"], "provider": d.model["provider"]},
                     access, approval={"approval_id": approval["approval_id"], "status": "pending"})
    return "proposed"


while True:
    msg = consumer.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        print("Kafka error:", msg.error())
        continue

    req = json.loads(msg.value())
    request_id = req["request_id"]
    role = req.get("role")
    trace = Trace(producer, PROFILE.name, request_id, role)
    trace.step("intake", "received", "info",
               f"Question received ({len(req['message'])} characters, not shown)"
               + (f" from role {role}" if role else ""))

    # 1. Input guardrails on the ORIGINAL text. Both layers run locally, so nothing
    #    leaves the system; unsafe requests never reach masking, tools, or the model.
    in_check = check_input(req["message"], PROFILE)
    in_layer = "rules"
    override = None
    if in_check.allowed:
        trace.step("intake", "input_rules", "pass", "No input rule matched")
        trace.start("intake", "input_classifier", f"Safety classifier ({PROFILE.classifier.model}) checking the question")
        in_check = classifier_check(req["message"], profile=PROFILE)
        in_layer = "classifier"
        # A narrow, profile-defined exemption (e.g. a pure record lookup flagged as
        # medical advice). Never for crisis. The answer is still fully checked.
        override = classifier_exemption(req["message"], in_check, PROFILE)
        if override:
            trace.step("intake", "input_classifier", "override",
                       f"Flagged as {in_check.category}, overridden: {override}",
                       category=in_check.category, exemption=override)
            in_check = GuardrailDecision(True, in_check.category,
                                         f"{in_check.reason}; overridden: {override}")
        elif in_check.allowed:
            trace.step("intake", "input_classifier", "pass", "Classifier: safe")
        else:
            trace.step("intake", "input_classifier", "block",
                       f"Blocked by the classifier: {in_check.category}", category=in_check.category)
    else:
        trace.step("intake", "input_rules", "block", f"Blocked by rule: {in_check.reason}",
                   category=in_check.category)
    audit_guardrail(request_id, "input", in_layer, in_check, role, override)
    if not in_check.allowed:
        publish_response(request_id, reply(in_check, role), [],
                         {"stage": "input", "layer": in_layer, "category": in_check.category})
        trace.step("intake", "reply", "info", f"Fixed {in_check.category} reply sent; nothing reached the model")
        consumer.commit(message=msg)
        print(f"Blocked at input by {in_layer} ({in_check.category}): {request_id}")
        continue

    # 1b. Role check (profiles with roles only). Unknown or missing role: no answer.
    access = None
    if PROFILE.roles:
        if role not in PROFILE.roles:
            audit.emit("audit.guardrails", {
                "request_id": request_id, "profile": PROFILE.name, "agent": AGENT_NAME, "role": role,
                "stage": "access", "layer": "roles", "allowed": False, "category": "role_required",
                "reason": "missing or unknown role",
            })
            publish_response(request_id, PROFILE.response("role_required"), [],
                             {"stage": "access", "layer": "roles", "category": "role_required"})
            trace.step("intake", "role_check", "block", "Missing or unknown role: no records, no answer")
            consumer.commit(message=msg)
            print(f"No valid role ({role!r}): {request_id}")
            continue
        seen = set(PROFILE.roles[role].data_classes)
        access = {"role": role, "role_title": PROFILE.roles[role].title,
                  "restricted": [c for c in PROFILE.data_classes if c not in seen]}
        not_seen = [PROFILE.data_classes[c] for c in access["restricted"]]
        if not not_seen:
            can_see = "may see all record data"
        elif len(not_seen) == len(PROFILE.data_classes):
            can_see = "may not see any record data"
        else:
            can_see = f"may not see {'; '.join(not_seen)}"
        trace.step("intake", "role_check", "pass", f"Role {access['role_title']}: {can_see}",
                   restricted=access["restricted"])

    # 2. Mask personal details before anything goes to tools or the model.
    masked_message, mapping = mask(req["message"], PROFILE)
    trace.step("intake", "masking", "info", masking_summary(mapping))

    # 2b. A request for a change? First ask OPA whether this role may ask for it at all.
    action = actions.detect(PROFILE, req["message"]) if PROFILE.actions else None
    if action:
        trace.step("action", "detected", "info", f"Change requested: {action.title}")
        allowed = action_policy(action, role, {})
        if not allowed.get("allowed"):
            reasons = allowed.get("reasons") or ["denied by policy"]
            trace.step("action", "policy", "block", f"Denied by policy: {'; '.join(reasons)}")
            audit_approval(request_id, role, "denied", action=action.name, reasons=reasons)
            publish_response(request_id, PROFILE.response("action_denied", role).format(reasons="; ".join(reasons)),
                             [], {"stage": "action", "layer": "policy", "category": "action_denied"},
                             access=access)
            consumer.commit(message=msg)
            print(f"Change denied ({action.name}, role {role}): {request_id}")
            continue

    # 3. Call the profile's tools through the gateway (policy-checked and audited).
    #    Tools get masked text, except tools the profile marks as receiving identifiers
    #    (a local records lookup needs the real name); the audit still records masked text.
    notes, tools_used = [], []
    for tool in PROFILE.agents.get(AGENT_NAME, ()):
        receives_ids = tool in PROFILE.identifier_tools
        agent = PROFILE.tools[tool].lane         # where its steps show in the trace (tools.yaml)
        trace.start(agent, tool, f"{tool} through the gateway (policy check)")
        try:
            result = call_tool(AGENT_NAME, tool, {"query": req["message"] if receives_ids else masked_message},
                               request_id, PROFILE,
                               audit_args={"query": masked_message} if receives_ids else None, role=role)
        except ToolCallDenied as e:
            print("Denied:", e)
            trace.step(agent, tool, "block", f"Denied by policy: role {role} may not use {tool}"
                       if PROFILE.roles else f"Denied by policy: {AGENT_NAME} may not use {tool}")
            continue
        found = result.get("results", [])
        summary = f"{len(found)} result(s)" + (f": {ids_text([n['id'] for n in found])}" if found else "")
        if tool in PROFILE.role_filtered_tools and access and access["restricted"]:
            summary += f"; not fetched for this role: {', '.join(access['restricted'])}"
        trace.step(agent, tool, "pass" if found else "info", summary, returned=len(found))
        if found:
            tools_used.append(tool)          # only tools that returned data count for routing
            notes += found
    if PROFILE.cite_sources:
        notes_text = "\n".join(f"- [{n['id']}] {n['title']}: {n['text']}" for n in notes)
    else:
        notes_text = "\n".join(f"- {n['title']}: {n['text']}" for n in notes)
    notes_text = notes_text or "- (no approved notes found)"
    if access and access["restricted"]:
        hidden = "; ".join(PROFILE.data_classes[c] for c in access["restricted"])
        notes_text += (f"\n\nAccess: the person asking is {access['role_title']}. Their role may not see: "
                       f"{hidden}. If they ask about these, say their role does not allow it and suggest "
                       "who on staff could help. Do not guess.")

    # 3b. A change request gets a proposal for a person to approve, not an answer.
    if action:
        outcome = propose(action, req, request_id, role, notes, notes_text, access, trace)
        consumer.commit(message=msg)
        print(f"Change request {action.name}: {outcome}: {request_id}")
        continue

    # 4. Decide which model answers (OPA policy, audited). External models only
    #    ever get masked text; the local model gets what the profile says.
    decision = route(PROFILE, mapping, tools_used, request_id, AGENT_NAME, role)
    trace.step("router", "route", "info",
               f"{decision.target.capitalize()} model {decision.model['name']} ({decision.model['provider']}): "
               f"{'; '.join(decision.reasons)}",
               target=decision.target, provider=decision.model["provider"], sent_masked=decision.send_masked)

    # 5. Ask the model.
    def ask(d: RouteDecision) -> str:
        user_text = masked_message if d.send_masked else req["message"]
        trace.start("answer", "model", f"Asking {d.model['name']} ({d.model['provider']}, {d.target}, sees "
                                       f"{'masked' if d.send_masked else 'original'} text, {len(notes)} note(s))")
        # Audit what the model saw. The audit never holds raw personal data:
        # when the local model sees the original, the masked text is recorded.
        audit.emit("audit.model_inputs", {
            "request_id": request_id,
            "profile": PROFILE.name,
            "agent": AGENT_NAME,
            "role": role,
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

    #    If an external model fails, try the profile's fallback models in order (still
    #    masked), then the local model. Each switch is audited and traced.
    options = [decision, *fallbacks(PROFILE, decision)]
    for i, option in enumerate(options):
        try:
            model_answer = ask(option)
            decision = option
            break
        except (requests.RequestException, ModelUnavailable) as e:
            if option.target != "external":
                raise
            nxt = options[i + 1]               # the local model is always last
            print(f"{option.model['provider']}/{option.model['name']} failed ({e}); "
                  f"trying {nxt.model['provider']}/{nxt.model['name']}: {request_id}")
            trace.step("answer", "model", "error",
                       f"{option.model['name']} ({option.model['provider']}) failed: {e}; "
                       f"trying {nxt.model['name']} ({nxt.model['provider']})")
            audit_route(PROFILE, nxt, request_id, AGENT_NAME, role, fallback=True)
    route_info = {"target": decision.target, "model": decision.model["name"],
                  "provider": decision.model["provider"]}
    trace.step("answer", "model", "info", f"Draft answer ready ({len(model_answer)} characters, not shown)")

    # 6. Output guardrails on the RESTORED answer, so masked names can't hide anything:
    #    rules, then the grounding check, then the classifier (which sees the question too).
    restored_answer = unmask(model_answer, mapping) if decision.send_masked else model_answer
    out_check = check_output(restored_answer, PROFILE)
    out_layer = "rules"
    trace.step("answer", "output_rules", "pass" if out_check.allowed else "block",
               "No output rule matched" if out_check.allowed else f"Blocked by rule: {out_check.reason}")
    if out_check.allowed and PROFILE.source_id_pattern is not None:
        out_check = check_citations(restored_answer, [n["id"] for n in notes], PROFILE)
        out_layer = "grounding"
        cited = sorted({m.group(0) for m in PROFILE.source_id_pattern.finditer(restored_answer)})
        trace.step("answer", "grounding", "pass" if out_check.allowed else "block",
                   (f"Cites {ids_text(cited)}, all given to the model" if cited else "Cites no sources")
                   if out_check.allowed else f"Blocked: {out_check.reason}", cited=cited)
    if out_check.allowed:
        trace.start("answer", "output_classifier", "Safety classifier checking the answer with the question")
        out_check = classifier_check(req["message"], restored_answer, PROFILE)
        out_layer = "classifier"
        trace.step("answer", "output_classifier", "pass" if out_check.allowed else "block",
                   "Classifier: safe" if out_check.allowed else f"Blocked by the classifier: {out_check.category}")
    audit_guardrail(request_id, "output", out_layer, out_check, role)
    if not out_check.allowed:
        publish_response(request_id, reply(out_check, role), [],
                         {"stage": "output", "layer": out_layer, "category": out_check.category},
                         route_info, access)
        trace.step("answer", "reply", "info", f"Answer withheld; fixed {out_check.category} reply sent")
        consumer.commit(message=msg)
        print(f"Blocked at output by {out_layer} ({out_check.category}): {request_id}")
        continue

    # 7. Publish the answer with its sources, then commit.
    sources = [{"id": n["id"], "title": n["title"]} for n in notes]
    publish_response(request_id, restored_answer, sources, None, route_info, access)
    trace.step("answer", "reply", "info", f"Answer sent with {len(sources)} source(s)",
               sources=[s["id"] for s in sources])
    consumer.commit(message=msg)
    print(f"Answered {request_id} for role {role} by {decision.target} model {decision.model['name']} "
          f"(masked {len(mapping)}, notes {[n['id'] for n in notes]})")