# Examples

Individual answers saved at each stage of development, to show how the system's
behaviour changes as safety layers are added.

| File | Stage | What it shows |
|---|---|---|
| baseline_001.json | Base model, system prompt only | Mentions medication despite instructions; invents placeholders; too long |
| baseline_002.json | Base model, improved system prompt | No medication mention, no placeholders, short; still assumed a wake-up time instead of asking |
All data is synthetic.