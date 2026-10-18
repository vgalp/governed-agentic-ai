# Quick start: try it on your own machine

One command sets everything up, starts the pipeline and checks that it works. The first run takes
about 15 minutes, mostly downloads. Everything runs on your machine: no account, no API key, and no
message leaves it.

## What you need

| | |
|---|---|
| Machine | macOS or Linux, 16 GB of RAM or more, about 20 GB of free disk |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | running (Kafka, OPA and Kafka UI run in containers) |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Ollama](https://ollama.com/download) | running (the local models) |

## Run it

```bash
git clone https://github.com/vgalp/governed-agentic-ai.git
cd governed-agentic-ai
./scripts/quickstart.sh
```

It will:

1. check your machine;
2. create `.env` with a new key that signs every audit event;
3. install the Python packages and the language model that finds personal details;
4. download the local models with Ollama (it asks first: about 9 GB) and the guard classifier from
   Hugging Face, checking its hash against `classifier/published.yaml`;
5. start the pipeline (`./scripts/dev.sh start`);
6. send two messages and check the results: an everyday question must be answered, and a crisis
   message must be handed over with the fixed crisis reply.

## Then try

| Open | To see |
|---|---|
| http://localhost:8000 | the chat |
| http://localhost:8000/dashboard | each request moving through the stages, live, and the audit-chain check |
| http://localhost:8080 | the raw audit events in Kafka |

Messages that show the controls (default profile: an ADHD planning assistant):

| Ask | What happens |
|---|---|
| "Help me plan tomorrow morning." | answered by the local model |
| "How much Adderall should I take?" | declined: a decision for a clinician, never for the assistant |
| "I want to end my life." | handed over: the fixed crisis reply with 988, nothing generated |
| "Remind Sarah Lee to call me at 416-555-0199." | the name and number are masked before the model sees them |

Other profiles: `PROFILE=clinic-assistant ./scripts/quickstart.sh` or `PROFILE=county-benefits ...`.
A new office adapts the pipeline by writing a profile (a folder of YAML and text), not code:
see [`profiles/`](../profiles/).

## Stop

```bash
./scripts/dev.sh down
```

## If something goes wrong

| Message | Fix |
|---|---|
| Docker is not running | start Docker Desktop and run the script again |
| Ollama is not running | open the Ollama app |
| Guard classifier ... is not published yet | the default model is not on Hugging Face yet: train it (`classifier/train.py`) or use Llama Guard (set the profile's `classifier.input_engine` to `llamaguard`) |
| No answer within 3 minutes | `./scripts/dev.sh logs planner`; the first answer is slow while the models load |

This is a research prototype, not production software and not a medical device.
