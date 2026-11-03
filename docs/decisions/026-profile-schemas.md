# 026: Profile files have schemas, and unknown keys are errors

Status: Accepted · Date: 2026-11-02

## Context
`profiles/loader.py` validates a profile with a thousand lines of hand-written checks, and does it
well for the semantic rules that need several files at once (a crisis flag can never be exempted,
a judge may not also answer, PERSON may never be restored for a tool). What it did not do is the
simple thing: refuse a key it does not know. A misspelled `on_emtpy:` was ignored and the feature
silently off; a category name written `{name: suicide, self-harm or abuse risk}` split at the comma
into a stray key nobody saw. For a layer whose promise is "configuration decides what the AI may
do", a silently ignored line of configuration is the wrong failure.

## Decision
`profiles/schema.py` describes the shape of every profile file as pydantic models — types,
required keys, enumerations — with `extra="forbid"` and strict types. The loader validates each
file against its model first and reports the field path (`tools.yaml: web_research: on_emtpy:
unknown key (misspelled?)`); keys that used to carry a specific warning keep it (`api_key: never
put an API key in a profile`). The semantic checks run after, unchanged.

`scripts/profile_schema.py` writes the models as JSON Schema to `docs/schemas/`; a test fails if
the files drift from the models. An office can validate a profile in an editor or a CI job
without running this code.

The first run found one real defect (the split category name in `general-assistant`).

## Consequences
- A profile mistake is caught at startup with its path, instead of at a request or never.
- Adding a setting means adding a field to a model, which documents it, types it and publishes it
  in the schema in one place. The loader keeps the cross-file rules; it does not shrink much yet.
- The schema is the contract for the configuration-only adoption claim: what an office may write
  is written down, and checked.
