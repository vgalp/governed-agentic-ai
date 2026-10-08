# 009: Human approval before changes

Status: Accepted · Date: 2026-10-07

## Context
Until now the assistant only read records and answered. Useful agents also change things:
move an appointment, update a record, send a message. A change made on a model's say-so is
the risk regulated organizations worry about most: a model can misread a request, be
manipulated by text it reads, or invent a detail. The endeavor commits to requiring human
approval before consequential actions.

## Decision
Language models propose and explain; deterministic, testable policy decides; people approve;
a separate agent acts. Every step is recorded.

1. **Propose.** A profile lists the changes the assistant may propose (`actions.yaml`). A
   request that matches an action's intent pattern is not answered. The local model fills in
   the action's fields as JSON, from the records it was given, and is told it changes nothing.
2. **Check.** Code validates the proposal: every field present, IDs only from the records
   the model was given (the same idea as the grounding check), dates that parse. The
   model's text is never shown or executed as is.
3. **Decide.** OPA (`policies/actions.rego`) decides whether the role may ask for this change
   and whether the facts are allowed (for appointments: opening hours, not in the past), and
   who must approve. The policy is checked twice: on the role alone before any records are
   read, and on the full proposal.
4. **Approve.** The proposal goes to `approvals.requested` (Kafka) with masked text and IDs
   only. A person decides on the dashboard (`approvals.decided`). Not decided within the
   action's time limit: expired. Nothing is changed in any of these cases.
5. **Act.** A separate executor agent asks OPA again: approved, by an approver role, not by
   the person who asked (separation of duties), not expired. Only then does it call the tool
   through the gateway, which checks that this agent and the approver's role may use it. An
   approval by someone who may not approve is refused, and the request stays open.
6. **Once.** The tool records each change with its approval ID in the same database
   transaction and refuses a second change for the same approval. On restart the executor
   rebuilds its view from Kafka without acting on old decisions.

The planner, which talks to the model, is never granted a tool that makes changes; the
profile loader refuses it. The executor never talks to a model and never reads the request.

Every step is audited on `audit.approvals` (hash-chained) and shown in the decision trace in
an `action` lane, from both processes, on the original request.

## Consequences
- The clinic profile can reschedule appointments with approval. New actions are profile
  configuration (fields, roles, limits), a policy rule if they need new facts, and a tool.
- The approver is chosen on the dashboard for this demo. A real deployment takes the
  approver's identity and role from sign-in, so separation of duties rests on real accounts.
- Approvals are shown with masked text and IDs. An approver who needs the full record looks
  it up with their own access.
- Not yet: approval by more than one person, notifying approvers, or undoing a change.