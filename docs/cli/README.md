# gaai: the command line and operator console (ADR 033)

```bash
./gaai                      # the console
./gaai --help               # the one-shot commands
```

The console opens with the platform's state: the active profile, how many services run, health, the
audit chain, approvals waiting, and whether the local assistant is ready. Then:

- **Slash commands** do one thing each (`/help` lists them; Tab completes commands, profiles and
  services; ↑ recalls earlier lines, kept across sessions).
- **Anything else** is asked of the local assistant, which uses the same commands to find out and
  answer: "why is the chart brief failing?", "switch to the Oracle clinic", "is the audit log intact?".

| Command | What it does |
|---|---|
| `/status` | the start screen again |
| `/services` | each service, its port, running / stopped / duplicated (stale copies) |
| `/health` | Kafka, the API's readers, planner, executor, delivery |
| `/audit [all]` | verify the tamper-evident audit chain |
| `/profiles` | every profile and where its records come from (database, or which FHIR server and sign-in) |
| `/use PROFILE` | switch: restart the platform with that profile (asks first) |
| `/start [PROFILE]`, `/restart [PROFILE]`, `/stop` | through `scripts/dev.sh`, with its output shown live (asks first) |
| `/logs SERVICE [N]` | the end of a service's log |
| `/approvals` | changes waiting for an approver (decided on the web page, by a signed-in approver) |
| `/ask ROLE QUESTION` | a question through the governed pipeline, as that role, with the answer and how it was produced |
| `/check PROFILE [PATIENT]` | the record-system check (`scripts/fhir_check.py`) |
| `/model [NAME]` | the assistant's model |
| `/clear` | the assistant forgets the conversation |

One-shot commands, for scripts: `./gaai status [--json]` (exit 0 only when every service runs),
`./gaai audit` (exit 0 only when the chain is intact), `./gaai ask --role clinician "…"`,
`./gaai start --profile clinic-oracle --yes`, `./gaai chat "is everything running?"`.

## The local assistant

A Qwen model served by [Ollama](https://ollama.com) on this computer. Set up once:

```bash
ollama pull qwen3:8b            # about 5 GB; or a smaller one: qwen3:4b
ollama serve                    # if Ollama is not already running (the Mac app runs it)
GAAI_MODEL=qwen3:4b ./gaai      # another model, or /model qwen3:4b inside the console
```

The console works without the assistant; only plain-words questions need it.

The assistant follows the platform's own rules:

- **It runs on this computer.** Nothing it reads leaves the machine.
- **Operations data only.** It can read service states, health, the audit result, logs (which hold
  masked text), profiles and approvals' status. It has no tool that reads a patient record or sends
  a question through the pipeline; for those, the operator uses `/ask` or the web page.
- **Reads freely, changes with a yes.** Start, stop, restart and switching profile are proposed and
  wait for `y`, the way changes in the pipeline wait for an approver.
- **It cannot approve.** Approvals stay with a signed-in approver on the web page.

## Without colour

Colour is used only on a terminal. `NO_COLOR=1` turns it off; a pipe gets plain text.
