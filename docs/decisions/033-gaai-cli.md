# 033: gaai, the command line and operator console

Status: Accepted · Date: 2026-11-12

## Context
Running the platform meant `scripts/dev.sh`, three web pages, the scripts in `scripts/` and `curl`
for the API. Every later phase (more record systems, the separate web UI, robustness work) is run,
tested and shown from a terminal. An operator also needs a quick answer to "is it all working, and
if not, why", which today means reading eleven log files.

## Decision
- **One command, `./gaai`,** with one-shot subcommands (`status`, `start`, `audit`, `ask`, `check`, …)
  for scripts and an interactive **console** for people: a start screen with the platform's state,
  slash commands with completion and history, and plain-words questions for a local assistant.
- **Standard library only** for the terminal (ANSI colour, panels, a spinner, readline). No new
  dependency; colour only on a terminal and never with `NO_COLOR`.
- **One set of operations** (`cli/platform.py`) used by the console, the one-shot commands and the
  assistant. Services are read from `dev.sh`'s list, so a new service appears in all three. Changes
  go through `dev.sh`, the same path a person uses.
- **A local operator assistant** (`cli/agent.py`): a Qwen model through Ollama's chat API with tool
  calling (default `qwen3:8b`, `GAAI_MODEL` to change). It holds itself to the platform's rules:
  local only; operations data only, with no tool that reads a patient record, sends a question
  through the pipeline or approves a change; reads freely, but every change (start, stop, restart,
  switch profile) waits for the operator's yes. Arguments a tool does not take are dropped (the
  record-system check reads no patient even if the model asks for one).

## Consequences
- The platform can be run and demonstrated from one terminal; `gaai status` and `gaai audit` have
  exit codes a monitor or CI can use.
- The assistant is as good as the local model. It cannot do harm the operator does not confirm, and
  it cannot see patient data, so a weaker model costs time, not safety.
- A full-screen dashboard (Textual) is not part of this: the console's start screen and `/status`
  cover the need for now without a dependency.
- Tested without a network (tests/test_cli.py): the fake Ollama scripts the model's tool calls,
  including a declined change, an unknown tool and a tool given an argument it does not take.
