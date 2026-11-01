# 021: A general-assistant profile for the public

Status: Accepted · Date: 2026-10-31

## Context
Every profile so far is for staff at one kind of office, built around that office's records.
Two things now point the other way:
- The OpenAI-compatible endpoint (ADR 020) lets an existing chatbot adopt the layer by changing
  its base URL. The obvious chatbot to put behind it is a general one for members of the public,
  and no profile fits that.
- The fixed crisis reply is written for staff ("a colleague will take over"). A member of the
  public in distress needs different words: no colleague exists, and the reply has to carry the
  whole hand-over on its own.

## Decision
`profiles/general-assistant` is an everyday assistant for the public, built from configuration
only, on the shared pipeline.
- **No records, no roles, no change tools.** One read-only knowledge tool with a few approved
  notes (where to get urgent help, how to prepare for an appointment). Everything else the model
  answers from its prompt.
- **Decisions it must not make** are the ones a member of the public would otherwise ask a
  chatbot for: diagnosis; medication doses, starting, stopping or changing a treatment; advice on
  the person's own legal case; which investment to buy or sell. The layer-1 rules cover the clear
  wordings, the guard classifier the rest, and the answer checks block a dose, a diagnosis or a
  trade in the model's reply.
- **Crisis** covers suicide, self-harm, abuse, neglect and harm from others, as in the office
  profiles, with an extra rule for a disclosure that someone is hurting the user.
- **The guard policy paragraph is the profile's own** (in `profile.yaml`, not in
  `classifier/policies.yaml`), so it never enters training data. The classifier reads it the way
  it reads the four unseen office policies in test set v5. How well it does that is measured, not
  assumed: `eval/probe_guard_policy.py` shows its reading during development, and the profile is
  a candidate for a future frozen test set.
- **The crisis reply is written for the public.** It says plainly that an assistant is not the
  support the person needs, names 988 (U.S.) and 9-8-8 (Canada), 911, and the domestic violence
  hotline, and does not use the staff wording. Pending clinical review, like the other replies.
- **Local only by default.** `external_allowed: false`. An organization that wants a hosted model
  for general questions turns it on in its copy of the profile; personal details still never
  leave the machine.

## Consequences
- An organization can put the layer in front of a public chatbot by starting from this profile and
  the endpoint, with no code.
- The guard classifier's false-alarm rate on everyday public conversation is higher than on
  office messages (benchmark v2: 3.4% of human-labelled safe prompts), so some ordinary messages
  will get a hand-over. That is known and measured; v7 is where it improves.
- The profile has no frozen test set yet. Until one exists, its numbers are development numbers
  only.
- The demo video uses this profile: the same chat app, pointed first at a model and then at the
  layer, on five fixed scenarios.
