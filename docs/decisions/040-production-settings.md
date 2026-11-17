# 040: A production setting, and what the audit may hold

Status: Accepted · Date: 2026-11-16 · Tested with stand-ins; not run in a production environment

## Context
A review of the system against a hospital's bar (16 November 2026) found development conveniences that
nothing stops from running beside real data, and one leak:

- Demo sign-in, the default, trusts whatever role a request claims.
- The identity stand-in for trying the gateway answers token checks from a local file.
- The gateway listens on plain http and has no limit per caller.
- A date of birth typed into a question was written to the audit trail. Masking for a model keeps
  dates on purpose (a visit date is clinical content), and the audit received the same text.
- The first request after a start loaded the masking analyzer and the models, and failed in two trials.

## Decision
- **`GOVERNED_ENV=production` is a statement that the deployment holds real data.** With it:
  - sign-in in demo mode is an error at start (`auth/sign_in.py`);
  - the identity stand-in does not run (`ops/dev_identity.py`);
  - the gateway does not start unless callers reach it over TLS (its own certificate, or
    `GATEWAY_TLS_AT_PROXY=1` to state that a proxy terminates it), and unless its token check is an
    https address that is not this machine (`gateway/endpoint.py`).
  Without the variable everything behaves as before, so development and the demos are unchanged.
- **The audit gets a second pass after masking** (`privacy.tokens.scrub_for_audit`, applied where a tool
  call's arguments are recorded): every date and every run of six or more digits becomes `[DATE]` or
  `[NUMBER]`. It is deliberately blunt. An audit record needs to show that a question was asked and what
  was decided, not the date in it.
- **Masking is measured** (`eval/run_masking.py`, `eval/datasets/masking_dev_v1.json`): 150 invented
  sentences, scored for what a model can still read and what the audit can still read, and for
  medication names masked by mistake.
- **One caller cannot take the gateway from the others**: a token bucket per client
  (`rate_limit` in outside.yaml), refusals audited.
- **The gateway loads everything at start** (`Endpoint.warm_up`). A part that cannot load stops the
  start.

## Consequences
- **Setting the variable is the operator's act.** Nothing detects real data. A deployment guide must
  set it, and the cloud modules should (not done here).
- **The scrub also removes dates and numbers that were not identifiers** (a visit date, a long dose
  string). The audit loses that detail on purpose.
- **Names still depend on the masking analyzer.** The scrub does nothing for a name the analyzer misses;
  the evaluation counts those.
- **The masking set is a development set**: ten templates, written by the author of the scrub. It is
  not evidence about real notes.
- **The rate limit is per process.** With several gateway processes each has its own count.
- **A 4,096-character tool answer seen in a trial is not explained.** No limit of that size exists in
  this code. It may be a coincidence; it is not fixed.
- Not built: validation of signed tokens at the gateway, a gateway-only container image, an
  end-to-end test in CI with the real policy engine, metrics and alerts.
