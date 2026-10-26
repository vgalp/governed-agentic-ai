# Tool-use accuracy set v1: protocol

Written and committed before the set is scored. Suggested by the independent reviewer (ADR 018).

## What it measures
For a staff question that has passed the input checks, the planner decides where it goes
(`agents/tool_choice.py`, `route`):
- **a change**: a request for a change (reschedule an appointment, release a held payment),
  which becomes a proposal for a person to approve;
- **a triggered tool**: a tool for certain questions only (medication review, medication list),
  which answers directly from source text;
- **general**: the general tools (approved policies, and records filtered by role).

Then it decides whether the role may go there, by the same rules OPA applies. This set scores both
decisions against labels.

Routing is decided by patterns in each profile's `rules.yaml`, not by a model. So this measures
how well those patterns match the way staff actually ask, and whether roles are enforced. It does
not measure the input checks, which can block a question before routing (the guard test sets do
that), or what a tool returns.

## Set
`tool_use_v1.jsonl`: 61 questions from staff in three profiles: clinic-assistant (33),
clinic-epic (11) and county-benefits (17). Each has a role, the right destination
(`expected_path`), and whether that role may take it (`expected_allowed`). Each question is one
of four kinds:

| Kind | Questions | What it tests |
|---|---|---|
| plain | 33 | typical wording |
| paraphrase | 10 | natural wording without the obvious keywords ("Could his warfarin and aspirin cause a problem together?", "Rosa's payment is stuck, can we get it issued?") |
| near_miss | 10 | sounds like a trigger but is not one ("What is our policy on drug reviews for new patients?", "Has the payment been released yet?") |
| role | 8 | the right tool or change, for a role that may not use it |

**Labels.** The label is where the question *should* go, not where today's patterns send it. A
question that asks for no change and no tool is `general`, even if it mentions a medication or a
payment. "Cancel the appointment" is `general`, because the clinic has no cancellation change;
the assistant should say so, not propose a reschedule.

Names are invented. The clinic and county names match the profiles' synthetic demo data. The Epic
patient is from Epic's public sandbox documentation.

## Who wrote it, and the limit that follows
Drafted with an AI assistant (Claude), which had read the routing patterns. Paraphrases and near
misses were written to reflect how staff ask, not to pass or fail the patterns, but the author
knew the patterns, so this set is not independent of them. It measures the patterns' coverage of
wording the author could imagine.

**v1b**, a second set of questions written by someone who has not read the patterns (the
independent reviewer is invited), is the stronger test. It will be frozen and scored the same way.

## Rules
1. Frozen by SHA-256 below. `eval/run_tool_use.py` refuses an unfrozen or changed set.
2. One run per routing configuration: the hash of the three profiles' rules, tools, roles,
   actions and profile files, plus the routing code. A result is never overwritten.
3. Never used to write or tune patterns. If the patterns change after v1 has been scored, the new
   configuration may be scored on v1 once, but the result must be reported as *after seeing v1*.
   The fair test of a change is v1b, or a v2.
4. A label found wrong after freezing is reported as excluded, with the reason. It is never edited.
5. Results are reported with their 95% Wilson intervals. They are reported by kind and by
   destination (precision and recall), with every wrong routing listed. Two kinds of error are
   counted separately:
   - **missed**: should have gone to a tool or a change, went to the general tools;
   - **false trigger**: a general question sent to a tool or a change.

   A missed medication review is not unsafe on its own: the question then reaches the model with
   the general tools, and the decision rules and guard classifier still block medication
   decisions. It does mean the clinician does not get the label-based review they asked for.

## Freeze
- `tool_use_v1.jsonl` SHA-256: pending
- Reviewed before freezing by: pending
