#!/usr/bin/env bash
# The governed gateway by itself, on one machine (ADR 039): OPA, the record tool servers and the
# governed MCP endpoint. No Kafka, no AI model, no chat page: the audit trail is written to files.
#
#   ./scripts/gateway.sh start     start OPA, the record tools, the identity stand-in and the gateway
#   ./scripts/gateway.sh stop      stop them (OPA's container keeps running; `docker compose stop opa` stops it)
#   ./scripts/gateway.sh status    what is running
#   ./scripts/gateway.sh verify    check the audit files for changes or gaps
#   ./scripts/gateway.sh logs [gateway|identity|fhir-records|medication-review|chart-brief]
#
#   PROFILE=clinic-epic ./scripts/gateway.sh start      another record system (default: clinic-oracle,
#                                                       whose open sandbox needs no credentials)
#   GATEWAY_IDENTITY=own ./scripts/gateway.sh start     do not start the development identity stand-in:
#                                                       outside.yaml auth.url points at your own system
#
# Gateway: http://127.0.0.1:8110/mcp   Guide: docs/gateway/README.md
# Stop the full stack first (./scripts/dev.sh stop): both use the same tool server ports.

set -euo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="logs"
PY=".venv/bin/python"
export PYTHONUNBUFFERED=1
export PROFILE="${PROFILE:-clinic-oracle}"
export AUDIT_SINK=file
export AUDIT_DIR="${AUDIT_DIR:-$PWD/logs/gateway-audit}"
export AUDIT_CHECKPOINT_DIR="${AUDIT_CHECKPOINT_DIR:-$PWD/logs/gateway-audit-checkpoints}"
IDENTITY="${GATEWAY_IDENTITY:-dev}"

# name | process pattern | module and arguments | port
SERVICES=(
  "medication-review|-m mcp_servers\.medication_review|mcp_servers.medication_review.server|8103"
  "fhir-records|-m mcp_servers\.fhir_records|mcp_servers.fhir_records.server|8104"
  "chart-brief|-m mcp_servers\.chart_brief|mcp_servers.chart_brief.server|8106"
)
# The stand-in listens where the profile's outside.yaml sends token checks (auth.url); 8120 as shipped.
IDENTITY_PORT="${GATEWAY_IDENTITY_PORT:-8120}"
[ "$IDENTITY" = "dev" ] && SERVICES+=("identity|-m ops\.dev_identity serve|ops.dev_identity serve --port $IDENTITY_PORT|$IDENTITY_PORT")
SERVICES+=("gateway|-m gateway\.endpoint|gateway.endpoint|8110")

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

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

load_env() {  # local secrets, made once and kept in .env (readable by its owner only)
  umask 077
  touch .env
  grep -q '^OPA_TOKEN=' .env || { echo "OPA_TOKEN=$(openssl rand -hex 24)" >> .env; ok "Made OPA_TOKEN in .env"; }
  grep -q '^GATEWAY_INTROSPECT_SECRET=' .env || {
    echo "GATEWAY_INTROSPECT_SECRET=$(openssl rand -hex 24)" >> .env
    ok "Made GATEWAY_INTROSPECT_SECRET in .env (the gateway sends it with each token check)"
  }
  set -a; . ./.env; set +a
  # Not made here: adding a key would make audit chains the full stack wrote without one fail its check.
  [ -n "${AUDIT_HMAC_KEY:-}" ] || warn "AUDIT_HMAC_KEY is not set: the audit files show changes, but not deliberate forgery"
}

check_prereqs() {
  say "Checking prerequisites"
  docker info >/dev/null 2>&1 || fail "Docker is not running (OPA runs in a container). Start Docker Desktop."
  ok "Docker"
  uv sync -q --inexact --group train   # train = torch, for the guard classifier that checks each request
  ok "Python environment"
  "$PY" -c "from profiles.loader import load_profile; import sys; sys.exit(load_profile().outside is None)" \
    || fail "Profile $PROFILE has no outside.yaml: it does not offer a gateway."
  if "$PY" -c "from profiles.loader import load_profile; import sys; sys.exit(load_profile().classifier.input_engine != 'guard_model')"; then
    guard=$("$PY" -c "from profiles.loader import load_profile; print(load_profile().classifier.guard_model)")
    [ -f "models/$guard/model.safetensors" ] || fail "Guard classifier models/$guard missing. Download it: $PY -m classifier.fetch $guard"
    ok "Guard classifier $guard"
  fi
  ok "Profile $PROFILE"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name pattern _ port <<< "$s"
    if [ "$(count_procs "$pattern")" -eq 0 ] && nc -z localhost "$port" >/dev/null 2>&1; then
      fail "Port $port ($name) is in use by another program (see it: lsof -nP -iTCP:$port -sTCP:LISTEN). If the full stack is running: ./scripts/dev.sh stop"
    fi
  done
}

start_opa() {
  say "Starting OPA"
  docker compose up -d opa >/dev/null
  wait_for_port 8181 30 && ok "OPA" || fail "OPA did not start"
}

stop_services() {
  say "Stopping the gateway"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name pattern _ _ <<< "$s"
    pkill -f -- "ops\.supervise $name --" 2>/dev/null || true
    pkill -f -- "$pattern" 2>/dev/null || true
    for _ in $(seq 10); do
      [ "$(count_procs "$pattern")" -eq 0 ] && break
      sleep 1
    done
    pkill -9 -f -- "ops\.supervise $name --" 2>/dev/null || true
    pkill -9 -f -- "$pattern" 2>/dev/null || true
    ok "$name stopped"
  done
}

start_services() {
  say "Starting the gateway for profile $PROFILE (logs in $LOG_DIR/)"
  mkdir -p "$LOG_DIR"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name _ args port <<< "$s"
    # shellcheck disable=SC2086  # args are split on purpose
    nohup "$PY" -m ops.supervise "$name" -- $args > "$LOG_DIR/gateway-$name.log" 2>&1 &
    wait_for_port "$port" 90 || { tail -20 "$LOG_DIR/gateway-$name.log"; fail "$name did not start"; }
    ok "$name"
  done
}

status() {
  say "Gateway (profile $PROFILE)"
  for s in "${SERVICES[@]}"; do
    IFS='|' read -r name pattern _ _ <<< "$s"
    n=$(count_procs "$pattern")
    if [ "$n" -eq 1 ]; then ok "$name running"
    elif [ "$n" -eq 0 ]; then warn "$name not running"
    else warn "$name has $n copies running. Run: ./scripts/gateway.sh restart"
    fi
  done
  nc -z localhost 8181 >/dev/null 2>&1 && ok "OPA answering" || warn "OPA not answering on port 8181"
  echo "  audit files: $AUDIT_DIR"
}

ready() {
  say "Ready.  Gateway: http://127.0.0.1:8110/mcp   Audit files: $AUDIT_DIR"
  if [ "$IDENTITY" = "dev" ]; then
    echo "  Callers are checked by the DEVELOPMENT identity stand-in. Try it (a caller is made for you):"
    echo "    $PY scripts/gateway_check.py                       # as a clinician"
    echo "    $PY scripts/gateway_check.py --as front_desk --tool search_records --query \"Wilm Smart, born 1990-09-15\""
  fi
}

case "${1:-}" in
  start)   load_env; check_prereqs; start_opa; stop_services; start_services; ready ;;
  stop)    stop_services ;;
  restart) load_env; stop_services; check_prereqs; start_opa; start_services; ready ;;
  status)  status ;;
  verify)  load_env; "$PY" -m audit.verify --dir "$AUDIT_DIR" ;;
  logs)    if [ -n "${2:-}" ]; then tail -f "$LOG_DIR/gateway-$2.log"; else tail -f "$LOG_DIR"/gateway-*.log; fi ;;
  *)       sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
