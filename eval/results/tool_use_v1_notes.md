# Tool-use accuracy v1: what the results show

Run 2026-10-25, routing configuration `f2284e448c44`, set `e23d65c972b8` (61 staff questions;
protocol: `eval/datasets/tool_use_v1_protocol.md`). Scored once. Nothing was changed after.

## Headline
| | Right destination | Right permission |
|---|---|---|
| All 61 | 45 (73.8%, 95% CI 61.6–83.2) | 58 (95.1%) |
| Plain wording (33) | **33 (100%)** | 33 (100%) |
| Role cases (8) | 8 (100%) | **8 (100%)** |
| Paraphrases (10) | **0 (0%)** | 10 (100%) |
| Near misses (10) | 4 (40%) | 7 (70%) |

1. **Permissions are enforced exactly.** Every time a question reached the right destination, the
   role's permission was right: 0 errors of that kind. Among the 8 role cases, front desk, nurse,
   compliance, intake and auditor roles were all refused the tools and changes they may not use.
   The 3 permission errors in the table all follow from a wrong destination (below), not from
   role enforcement.
2. **Routing works on standard wording and fails on natural paraphrase.** The patterns route
   every typical request correctly, but none of the 10 paraphrases. They recognize specific verbs
   and nouns ("interact", "medication review", "reschedule", "release"), so "Could his warfarin and
   aspirin cause a problem together?", "can you book him for Wednesday instead?" and "can we get it
   issued?" all fall through to the general tools.
3. **Keywords without intent cause false triggers.** 6 of 10 near misses were sent to a tool or a
   change because they contain the keyword:
   - "Why did the visit get moved last week?" became a reschedule proposal;
   - "Has the payment been released yet?" became a release proposal;
   - "How long does a medication review appointment take?" went to the medication review tool;
   - "What does our policy say about medication reconciliation?" went to the medication list tool.

## What each error does in use
| Error | Count | Effect | Safe? |
|---|---|---|---|
| Missed medication review or list | 6 | The question goes to the model with the general tools. Medication decisions are still blocked by the decision rules and the guard classifier, but the clinician does not get the label-based review or the direct list they asked for | Safe, unhelpful |
| Missed change request | 4 | The assistant answers instead of preparing a proposal; nothing is changed | Safe, unhelpful |
| False trigger to a change | 3 | A question becomes a proposal no one asked for. The model may report it unclear, or a proposal goes to the approval queue. Nothing changes without a person approving | Safe, confusing, adds noise for approvers |
| False trigger to a tool the role lacks | 2 | A policy question gets "your role may not use the medication review" | Safe, confusing |

**None of the 16 errors let anything happen that should not.** The controls that matter for
safety held:
- roles were enforced exactly;
- every change still needs a person's approval;
- medication decisions are blocked by separate layers.

**What fails is usefulness.** Staff who word a request naturally do not get the tool or proposal
they meant, and some ordinary questions trigger the wrong path.

## Why
Routing by keyword patterns was chosen for predictability: no model decides where a question goes,
and every route can be read in `rules.yaml`. This result is the cost of that choice. A pattern
only knows the words its author thought of, and cannot tell "move the appointment" (a request)
from "the appointment was moved" (a question about the past).

## Next
- **Do not tune the patterns against this set.** It would raise this score and prove nothing; the
  protocol forbids it.
- **A router that reads intent.** The local model chooses among the profile's destinations, using
  the descriptions already in `tools.yaml` and `actions.yaml`, so a new office still configures it
  without code. It answers in a fixed format.
  - The patterns stay as the fallback when the model is unavailable.
  - The router only picks a destination: it grants nothing. OPA still decides permissions, and a
    change still needs approval.
  - Its cost (one more model call) is measured by ADR 018.
- **Evaluation of that router:**
  - on v1 once, reported as "after seeing v1";
  - on **v1b**, questions written by someone who has not seen the patterns or v1. This is the
    fair test.
