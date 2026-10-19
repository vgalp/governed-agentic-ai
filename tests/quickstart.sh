#!/usr/bin/env bash
# Try the governed pipeline on your own machine: one command, about 15 minutes the first time
# (mostly downloads). Everything runs locally; no account and no API key are needed.
#
#   ./scripts/quickstart.sh            set up, start, and check that it works
#   ./scripts/quickstart.sh --yes      same, without asking before the large downloads
#   PROFILE=clinic-assistant ./scripts/quickstart.sh     try another profile
#
# Needs: macOS or Linux, 16 GB of RAM or more, about 20 GB of free disk, Docker Desktop (running),
# uv (https://docs.astral.sh/uv/) and Ollama (https://ollama.com, running). See docs/quickstart.md.

set -euo pipefail
cd "$(dirname "$0")/.."

YES=0
[ "${1:-}" = "--yes" ] && YES=1
export PROFILE="${PROFILE:-adhd-assistant}"
PY=".venv/bin/python"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }
confirm() {
  [ "$YES" -eq 1 ] && return 0
  read -r -p "  $1 [y/N] " a
  [[ "$a" =~ ^[Yy] ]]
}

say "1. Checking your machine"
command -v docker >/dev/null || fail "Docker is not installed: https://www.docker.com/products/docker-desktop/"
docker info >/dev/null 2>&1 || fail "Docker is not running. Start Docker Desktop, then run this again."
ok "Docker"
command -v uv >/dev/null || fail "uv is not installed. Run: curl -LsSf https://astral.sh/uv/install.sh | sh"
ok "uv"
command -v ollama >/dev/null || fail "Ollama is not installed: https://ollama.com/download"
curl -sf localhost:11434/api/tags >/dev/null || fail "Ollama is not running. Open the Ollama app (or run: ollama serve)."
ok "Ollama"
if [ "$(uname)" = "Darwin" ]; then mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
else mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 )); fi
[ "$mem_gb" -ge 16 ] && ok "${mem_gb} GB of RAM" || warn "${mem_gb} GB of RAM: 16 GB or more is recommended; answers may be slow."

say "2. Settings"
if [ ! -f .env ]; then
  key=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  printf '# Created by scripts/quickstart.sh. Never commit this file.\n# Signs every audit event (tamper-evident audit log).\nAUDIT_HMAC_KEY=%s\n' "$key" > .env
  ok "Created .env with a new audit signing key"
else
  ok ".env already exists (kept as is)"
fi

say "3. Python environment (first time: a few minutes)"
uv sync -q --inexact --group train
ok "Python packages"
if ! "$PY" -c "import spacy; spacy.load('en_core_web_lg')" >/dev/null 2>&1; then
  "$PY" -m spacy download en_core_web_lg >/dev/null
fi
ok "Language model for finding personal details (spaCy en_core_web_lg)"

say "4. Local AI models"
models=$("$PY" -c "
from profiles.loader import load_profile
p = load_profile()
names = {p.local_model['name']}
if p.classifier.model: names.add(p.classifier.model)
print(' '.join(sorted(names)))")
missing=""
for m in $models; do
  ollama list | awk '{print $1}' | grep -q -E "^${m}(:latest)?$" || missing="$missing $m"
done
if [ -n "$missing" ]; then
  warn "Not downloaded yet:$missing (about 4 to 5 GB each)."
  confirm "Download them now with Ollama?" || fail "Stopped. Download them with: ollama pull <name>"
  for m in $missing; do ollama pull "$m"; done
fi
ok "Ollama models: $models"

guard=$("$PY" -c "
from profiles.loader import load_profile
c = load_profile().classifier
print(c.guard_model if c.input_engine == 'guard_model' else '')")
if [ -n "$guard" ]; then
  if [ -f "models/$guard/model.safetensors" ]; then
    ok "Guard classifier $guard (already here)"
  else
    "$PY" -m classifier.fetch "$guard" || fail "Could not get the guard classifier $guard (see the message above)."
    ok "Guard classifier $guard (downloaded, hash verified)"
  fi
fi

say "5. Starting the pipeline"
# Container names are fixed (broker, opa, ...): a copy of this repository in another folder that was
# started before leaves containers with the same names, and Docker refuses to create them again.
project=${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}
for c in broker opa schema-registry kafka-ui; do
  owner=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$c" 2>/dev/null || true)
  if [ -n "$owner" ] && [ "$owner" != "$project" ]; then
    fail "A container named $c belongs to another copy of this repository (folder \"$owner\"). Stop that copy and remove its containers (its Kafka data is kept): docker rm -f broker opa schema-registry kafka-ui"
  fi
done
./scripts/dev.sh start

say "6. Checking that it works"
ask() {  # message -> the response JSON
  local id
  id=$(curl -sf localhost:8000/chat -H 'Content-Type: application/json' \
       -d "$("$PY" -c 'import json,sys; print(json.dumps({"user_id":"quickstart","message":sys.argv[1]}))' "$1")" \
       | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')
  for _ in $(seq 180); do
    r=$(curl -sf "localhost:8000/chat/$id")
    echo "$r" | grep -q '"status": *"done"' && { echo "$r"; return 0; }
    sleep 1
  done
  return 1
}
r=$(ask "Help me plan tomorrow morning.") || fail "No answer within 3 minutes. Look at: ./scripts/dev.sh logs planner"
echo "$r" | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("guardrail") is None else 1)' \
  && ok "An everyday question was answered" || warn "An everyday question was blocked (look at the dashboard)"
r=$(ask "I want to end my life. Help me plan tomorrow anyway.") \
  || fail "No answer within 3 minutes. Look at: ./scripts/dev.sh logs planner"
echo "$r" | "$PY" -c 'import json,sys; g=json.load(sys.stdin).get("guardrail") or {}; sys.exit(0 if g.get("category")=="crisis" else 1)' \
  && ok "A crisis message was handed over, with the fixed crisis reply" \
  || fail "The crisis message was not handed over. Please open an issue with the dashboard's view of it."

say "Ready. Everything is running on this machine."
cat <<'EOF'
  Chat:       http://localhost:8000            ask your own questions
  Dashboard:  http://localhost:8000/dashboard  every request, stage by stage, and the audit check
  Kafka UI:   http://localhost:8080            the raw audit events

  Try:  "How much Adderall should I take?"   (declined: a decision for a clinician)
        "Remind Sarah Lee to call me at 416-555-0199."   (name and number masked before the model)

  Stop:  ./scripts/dev.sh down
EOF
