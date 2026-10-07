#!/usr/bin/env bash
# Start, stop and check the local development stack.
#
#   ./scripts/dev.sh start     start infrastructure and all services (stops old copies first)
#   ./scripts/dev.sh stop      stop the Python services
#   ./scripts/dev.sh down      stop the Python services and the Docker containers
#   ./scripts/dev.sh restart   stop, then start
#   ./scripts/dev.sh status    show what is running, and warn about duplicate processes
#   ./scripts/dev.sh logs [knowledge-base|api|planner]   follow logs
#
#   PROFILE=<name> ./scripts/dev.sh start   run another profile (default: adhd-assistant)
#
# Chat UI: http://localhost:8000   Kafka UI: http://localhost:8080

set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a   # load local secrets such as AUDIT_HMAC_KEY

LOG_DIR="logs"
PY=".venv/bin/python"
TOPICS="chat.requests chat.responses audit.model_inputs audit.tool_calls audit.guardrails audit.routing"
MODELS="mistral llama-guard3:8b"
export PYTHONUNBUFFERED=1   # write logs immediately
export PROFILE="${PROFILE:-adhd-assistant}"   # which profile every service loads (see profiles/)

# name | process pattern | module and arguments | port to wait for ("" = none)
SERVICES=(
  "knowledge-base|-m mcp_servers\.knowledge_base|mcp_servers.knowledge_base.server|8100"
  "api|-m uvicorn api\.main|uvicorn api.main:app --port 8000|8000"
  "planner|-m agents\.planner|agents.planner|"
)

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

# Count running Python processes for a service (ignores "uv run" and shell wrappers).
count_procs() {
  ps -Ao command= | grep -E -- "$1" | grep -v -E '^(uv |grep |[^ ]*bash )' | grep -c . || true
}

wait_for_port() {  # port, seconds
  for _ in $(seq "$2"); do
    nc -z localhost "$1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

check_prereqs() {
  say "Checking prerequisites"
  docker info >/dev/null 2>&1 || fail "Docker is not running. Start Docker Desktop."
  ok "Docker"
  curl -sf localhost:11434/api/tags >/dev/null || fail "Ollama is not running. Open the Ollama app or run: ollama serve"
  for m in $MODELS; do
    ollama list | awk '{print $1}' | grep -q -E "^${m}(:latest)?$" || fail "Model $m missing. Run: ollama pull $m"
  done
  ok "Ollama ($MODELS)"
  uv sync -q
  ok "Python environment"
  "$PY" -m profiles.loader >/dev/null || { "$PY" -m profiles.loader; fail "Profile check failed"; }
  ok "Profile $PROFILE"
}

start_infra() {
  say "Starting infrastructure"
  docker compose up -d >/dev/null
  for _ in $(seq 60); do
    docker exec broker kafka-topics --bootstrap-server broker:29092 --list >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec broker kafka-topics --bootstrap-server broker:29092 --list >/dev/null 2>&1 || fail "Kafka did not start"
  for t in $TOPICS; do
    docker exec broker kafka-topics --bootstrap-server broker:29092 --create --if-not-exists --topic "$t" >/dev/null
  done
  ok "Kafka, topics"
  wait_for_port 8181 30 && ok "OPA" || fail "OPA did not start"
}

stop_services() {
  say "Stopping services"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name pattern _ _ <<< "$s"
    pkill -f -- "$pattern" 2>/dev/null || true
    for _ in $(seq 10); do
      [ "$(count_procs "$pattern")" -eq 0 ] && break
      sleep 1
    done
    pkill -9 -f -- "$pattern" 2>/dev/null || true
    ok "$name stopped"
  done
}

start_services() {
  say "Starting services (logs in $LOG_DIR/)"
  mkdir -p "$LOG_DIR"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name _ args port <<< "$s"
    # shellcheck disable=SC2086  # args are split on purpose
    nohup "$PY" -m $args > "$LOG_DIR/$name.log" 2>&1 &
    if [ -n "$port" ]; then
      wait_for_port "$port" 60 || { tail -20 "$LOG_DIR/$name.log"; fail "$name did not start"; }
    else
      for _ in $(seq 90); do
        grep -q "listening" "$LOG_DIR/$name.log" 2>/dev/null && break
        sleep 1
      done
      grep -q "listening" "$LOG_DIR/$name.log" || { tail -20 "$LOG_DIR/$name.log"; fail "$name did not start"; }
    fi
    ok "$name"
  done
}

status() {
  say "Services"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name pattern _ _ <<< "$s"
    n=$(count_procs "$pattern")
    if [ "$n" -eq 1 ]; then ok "$name running"
    elif [ "$n" -eq 0 ]; then warn "$name not running"
    else warn "$name has $n copies running (stale code risk). Run: ./scripts/dev.sh restart"
    fi
  done
  say "Containers"
  docker compose ps --format '  {{.Service}}: {{.State}}' 2>/dev/null || warn "Docker not running"
}

case "${1:-}" in
  start)
    check_prereqs; start_infra; stop_services; start_services
    say "Ready.  Chat UI: http://localhost:8000   Kafka UI: http://localhost:8080"
    ;;
  stop)    stop_services ;;
  down)    stop_services; say "Stopping containers"; docker compose stop >/dev/null; ok "containers stopped" ;;
  restart) stop_services; check_prereqs; start_infra; start_services
           say "Ready.  Chat UI: http://localhost:8000" ;;
  status)  status ;;
  logs)    if [ -n "${2:-}" ]; then tail -f "$LOG_DIR/$2.log"; else tail -f "$LOG_DIR"/*.log; fi ;;
  *)       sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac