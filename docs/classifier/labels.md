# Guard classifier: labels

Status: Draft for review · Date: 2026-10-09

The guard classifier is a small, fast model that decides whether a message may go to
the assistant, needs a fixed reply because it asks for a professional decision about a
person, or may involve someone at risk. It is layer 3 of the guardrails: the rules
(layer 1), OPA and human approval stay as they are. A profile chooses it instead of, or
next to, the Llama Guard classifier (layer 2).

## Input and output

- **Input:** the profile's guard policy (a short paragraph in the profile saying what the
  assistant is for and what counts as a decision there) and the user's message. One
  model serves every profile, because the policy says what a decision is in that setting.
- **Output:** one label and a confidence score between 0 and 1.

## Labels

| Label | Meaning |
| --- | --- |
| `allow` | A lookup, a general question, a change request, or ordinary help. |
| `decision` | Asks the assistant to make or predict a professional judgment about a person: a dose, a diagnosis, eligibility, honesty. |
| `crisis` | Someone may be at risk of harm, stated directly or indirectly, about themselves or someone else. |

## Examples

| Profile | `allow` | `decision` | `crisis` |
| --- | --- | --- | --- |
| ADHD assistant | "Help me plan tomorrow" | "Do I have ADHD?" · "Can I take two Adderall?" | "I've started giving away my things" |
| Clinic staff | "When is Chester's next appointment?" | "Should we increase his dose?" | "The patient said he doesn't want to wake up" |
| County caseworkers | "What income has Rosa Delgado reported?" | "Is Tom Brandt eligible?" · "Is Lena lying about her income?" | "She told me she's being hit at home" |

## Rules for hard cases

1. **Crisis always wins.** A planning request that also says "everyone would be better off
   without me" is `crisis`.
2. **Reported crisis counts.** "The client said he wants to die" is `crisis`, whoever writes it.
3. **Figurative language does not.** "This deadline is killing me" and "I'm dying to finish
   this" are `allow`.
4. **Reading a record is not deciding.** Asking what a record already says is `allow`:
   "What's the status of Tom's case?" (even if it says denied), "Is his income verified?",
   "What medications is Chester on?"
5. **General questions about no one in particular are `allow`**, such as "Are college
   students eligible for SNAP?". The exception: **medication amounts are always
   `decision` in health profiles**, even in general form ("What's a normal Adderall dose?").
6. **Asking to predict or judge is `decision`:** "Will she likely be approved?", "Does this
   sound like ADHD?", "Does this look suspicious?"
7. **Change requests are `allow`.** "Move Chester's appointment to Friday" and "Release
   Lena's held payment" go to the approval step, which handles them.
8. **When unsure, the safer side.** Between `allow` and `decision`, label `decision`;
   between `decision` and `crisis`, label `crisis`. Thresholds on the confidence score
   are tuned later, on the development data only.

## How a label becomes a reply

- `crisis`: the profile's crisis reply.
- `decision`: the reply of the rule that matched, if one did, otherwise the profile's
  default category (for example medication or eligibility_decision). This is how the
  Llama Guard classifier is wired today.
- `allow`: the request continues through masking, tools, routing and the model.

## Data rules

- **Synthetic only.** No real people, patients, clients or cases.
- **The test set is written and frozen before any training data exists**, with its
  SHA-256 recorded, and never used for tuning or training (see the test set protocol).
- **The frozen red-team sets** (`redteam_v1`, `redteam_v2`) are never used for training
  or tuning either.
- Training examples cover the three existing profiles. The test set also includes a
  fourth profile the model never sees in training, to check that it generalizes.

## Decisions

- Three labels, not one per decision type: simpler, and it works for new profiles.
  Finer labels can be added later if the data supports them.
- Base model: ModernBERT-base (Apache 2.0). A fine-tuned small chat model
  (Qwen2.5-1.5B-Instruct or SmolLM2-1.7B, Apache 2.0, LoRA with MLX) is trained later
  on the same data and compared on the same test set.