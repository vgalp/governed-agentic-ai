# 037: A continuous training loop for the guard and the answering model

Status: Accepted · Date: 2026-11-13 · Guard loop run twice (run `trial1`); round 1 promoted as guard-modernbert-v7 on 2026-11-14. Answer loop not yet run

## Context
The guard classifier and the answering model are the parts of the assistant that make it more than
plumbing, and both are behind:

- Guard v6.1 catches 62.7% of crisis messages on the independent benchmark v2, and 13.7% when they
  are wrapped in role-play. v4 caught 86.7%, but gave a third of safe messages the crisis reply.
  Each release was trained by hand, and fixed one side by breaking the other
  (eval/results/crisis_benchmark_v2_notes.md).
- The answering model is Mistral 7B (2024). Routing is keyword rules, which sent 0 of 10 paraphrased
  questions to the right tool (eval/results/tool_use_v1_notes.md).

Improving either by hand is slow: generate, clean, review, train, calibrate, score, decide what to
write next. This ADR makes that a loop that runs until targets are met, with a frontier model writing
and labelling the data.

## Decision
`modelops/` (run with `python -m modelops`) runs rounds for each model, recorded in an append-only
ledger (`modelops/runs/<run>/ledger.jsonl`). Settings, targets and limits are in
`modelops/loop.yaml`, validated with bounds.

**Guard round** (`modelops/guard_loop.py`):
1. **Plan.** Each attack kind per profile gets a batch, and more goes where the gate sets showed the
   most mistakes (classifier/improve.py's planner, fed from the ledger).
2. **Gate set.** The gate set is written first, cleaned, and saved under classifier/data/gate/.
   classifier/clean.py keeps every training message away from it, so it is never trained on.
3. **Write training data.** The frontier writes this round's training messages.
4. **Label it.** The frontier labels the messages again blind, and a local model labels them
   independently.
   - All three agree: the message is trained on.
   - The frontier disagrees with itself: the message is dropped.
   - The frontier agrees but the local model does not: the message goes to the reviewer
     (`modelops/reviewer.py`), which sees the policy, the rules and both labels:
     - sure, and the label as written: trained on;
     - sure, and a different label: dropped;
     - it sees a crisis that was not written as one, it is unsure, or it raises a concern: flagged
       for a person.
   - The reviewer also checks a sample of agreed messages (`reviewer_sample_rate`). One it doubts
     is not trained on.
   - The gate set uses the same rules.
5. **Sanitize, then train and calibrate.**
   - Sanitizing drops contact details and identifiers, then applies clean.py's rules.
   - Training and calibration use the existing scripts, one new model folder per round.
6. **Measure.** Score on every gate set of the run, and on dev.

**Answer round** (`modelops/answer_loop.py`):
1. **Select a base.** `select` scores each candidate model, unchanged, on one gate set.
2. **Gate set.** Each round writes and holds out its own gate set.
3. **Write examples.** The frontier writes two kinds:
   - Routing: the model answers `{"path": ...}`, the same paths `agents/tool_choice.route` returns.
   - Grounded answers, of four kinds: answered from the notes, not in the record, declined decision,
     and quoted dose.
4. **Check them.** An example is kept only if it passes the checks a served answer would face:
   - it cites only the notes it was given;
   - nothing gives a medication amount it is not quoting from a note;
   - it has no contact details;
   - its destination exists.
5. **Train.** Each round trains a LoRA from the base on every accepted example so far (mlx_lm, or
   `modelops/peft_train.py` on CUDA). The adapter is merged and imported into Ollama under a new tag.
   The prompt format is the pipeline's own: `answer_system` and `notes_text` moved into
   agents/pipeline/stages.py and are shared, so the model is trained on what it is served.
6. **Measure.** Rule checks decide. The frontier's 1–5 grade is reported beside them.

**Stopping.** A loop stops when:
- every target is met;
- `max_rounds` is reached;
- `max_usd` of frontier spend is reached (`modelops/frontier.py`); or
- `patience` rounds in a row did not beat the best score.

Frontier replies are cached by request, so a resumed or repeated round costs nothing.

**Promotion is a person's step** (`python -m modelops promote`). The loop never changes which guard
or model the profiles use. Promotion requires:
1. **The gate targets met.**
2. **Every row the reviewer flagged, in the rounds that trained the model, labelled by a person.**
   A person sees only what the reviewer was unsure about, concerned by, or read as a possible
   missed crisis. Everything it settled is recorded in each round's `reviewed.csv`.
3. **The sealed checks, each run once.**
   - For the guard: frozen test set v5 and crisis benchmark v2.
   - For the answering model: the frozen tool-use set, routed by the model, with permissions still
     decided by the profiles' rules.

## Guarding against fooling ourselves
- **What the gate numbers can and cannot prove.** The gate sets are written by the same frontier
  model as the training data, and the planner aims at their weak cells. So gate numbers decide when
  the loop stops, never whether a model is better.
- **v2 stays independent.** Benchmark v2 was written by other people and is never read by the loop.
  It stays an independent test and is the primary result at promotion.
- **The frozen test sets lose some of their value.** classifier/generate.py trained on local-model
  text because those sets were drafted with Claude. Training now on frontier text puts them closer
  in style to the training data. A benchmark v3 from other public sources should be built before v2
  has been used for a decision.
- **Two labellers, a reviewer, and a person for the doubts.** A frontier label alone never decides a
  training label: the local model must agree, or the reviewer must settle it with high confidence.
  Whatever it cannot settle goes to a person, and promotion waits for that review.
- **The reviewer should not be the writer's model.** The same model can share a blind spot.
  loop.yaml sets a different one under `reviewer:` (Claude Sonnet reviewing Claude Opus); without
  that entry the frontier model reviews its own writing, which is the weakest link in the chain.
- **Every round is recorded with its data hashes.** Nothing is overwritten.

## Consequences
- One command per model runs until the targets or the limits.
  - Cost is bounded by `max_usd` (default $150 per run).
  - Time is bounded by `max_rounds`.
- It still needs:
  - a frontier key (ANTHROPIC_API_KEY);
  - Ollama with the local judge and the candidates;
  - for answers: Apple silicon with mlx_lm, or a CUDA machine with torch, transformers and peft.
- Each candidate's Ollama tag and Hugging Face id in loop.yaml must be checked before a run. If
  Ollama cannot import a merged architecture, it is served with llama.cpp or vLLM instead
  (profile provider `openai_compatible`).
- **Check the frontier provider's terms** for using its outputs to train other models before a run.
- Not built here:
  - a model-based router in the planner itself (the loop trains and scores one; wiring it in is the
    next step);
  - benchmark v3;
  - a scheduler (runs are started by a person, or by a scheduled task).

## First run and promotion (2026-11-14)
Run `trial1`, two guard rounds, about $30 of frontier calls. Details and tables:
eval/results/guard_v7_notes.md.

- **Round 1 was promoted as guard-modernbert-v7** and is the profiles' guard. On benchmark v2 it
  catches 74.9% of crisis messages (v6.1: 62.7%) and gives the crisis reply to 9.2% of the others
  (v6.1: 3.4%). On test set v5 the two are level.
- **It was promoted outside `modelops promote`.** Round 1 missed two gate targets narrowly (false
  crisis 0.055 against 0.05, and dev macro F1), so the command refused it. A person made the decision
  on the independent benchmark and recorded it here. The targets were not changed.
- **The gate sets overstated the result:** 98.4% crisis recall on the gate, 74.9% on benchmark v2.
  Gate messages come from the same frontier model as the training data. They compare rounds; they do
  not predict an independent score.
- **Round 2 added nothing** on the gate, and its third epoch collapsed (cause not established; the
  machine was short of memory).
- **Changes made during the run:** the reviewer is a different model from the writer (Claude Sonnet
  reviewing Claude Opus); doubt is read from the reviewer's confidence and `needs_person`, not from its
  explanation; calibration takes the loop's false-alarm target; a declined request is skipped and
  counted; a round that is run again keeps its own gate messages (the first round 2 lost most of its
  gate set to this).
- **Benchmark v2 has now been used for a promotion decision.** Build benchmark v3 before the next one.
