#!/usr/bin/env bash
# The secured local Kafka (audit SC-3, ADR 031): TLS, one SCRAM identity per service, and ACLs from
# ops/kafka_access.yaml. Normally run for you by:
#
#   GOVERNED_KAFKA_SECURE=on ./scripts/dev.sh start
#
#   ./scripts/kafka_secure.sh prepare   make certs/kafka.pem and .kafka.env (passwords, made once)
#   ./scripts/kafka_secure.sh apply     add the users and ACLs to the running broker (skipped when
#                                       nothing changed since the last time)
#   ./scripts/kafka_secure.sh check     prove it: a wrong password, plaintext and a forbidden write
#                                       are refused; a permitted connection works
#   ./scripts/kafka_secure.sh show      list the ACLs the broker holds
#
# .kafka.env holds the passwords (mode 600, never committed). To use the secured broker from your own
# shell, for example for python -m audit.verify:  set -a; . ./.kafka.env; set +a

set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
ENV_FILE=".kafka.env"
ADMIN="localhost:29094"          # the broker container's own loopback listener (no network access)
USERS="api planner executor endpoint audit-verify kafka-ui"
STAMP="logs/.kafka-access-applied"

tag() { echo "$1" | tr 'a-z-' 'A-Z_'; }

prepare() {
  bash scripts/certs.sh kafka >/dev/null
  umask 077
  if [ ! -f "$ENV_FILE" ]; then
    printf '%s\n' "# Secured local Kafka (scripts/kafka_secure.sh, ADR 031). Never commit this file." \
      "KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092" "KAFKA_SECURITY_PROTOCOL=SASL_SSL" \
      "KAFKA_SASL_MECHANISM=SCRAM-SHA-512" "KAFKA_CA_FILE=certs/ca.crt" > "$ENV_FILE"
  fi
  for u in $USERS; do
    t="$(tag "$u")"
    grep -q "^KAFKA_PASSWORD_$t=" "$ENV_FILE" ||
      printf 'KAFKA_USERNAME_%s=%s\nKAFKA_PASSWORD_%s=%s\n' "$t" "$u" "$t" "$(openssl rand -hex 24)" >> "$ENV_FILE"
  done
  chmod 600 "$ENV_FILE"
}

apply() {
  [ -f "$ENV_FILE" ] || prepare
  set -a; . "./$ENV_FILE"; set +a
  stamp="$(cat ops/kafka_access.yaml "$ENV_FILE" | openssl dgst -sha256 | awk '{print $NF}')"
  have="$(docker exec broker kafka-configs --bootstrap-server "$ADMIN" --describe --entity-type users 2>/dev/null || true)"
  missing=0
  for u in $USERS; do grep -q "user-principal '$u'" <<< "$have" || missing=1; done
  if [ "$missing" = 0 ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$stamp" ]; then
    echo "Kafka users and ACLs already in place"
    return 0
  fi
  for u in $USERS; do
    t="$(tag "$u")"
    pw_var="KAFKA_PASSWORD_$t"
    # The password goes through a file inside the container, never a command line.
    printf 'SCRAM-SHA-512=[iterations=8192,password=%s]\n' "${!pw_var}" |
      docker exec -i broker sh -c 'umask 077; cat > /tmp/scram.properties'
    docker exec broker sh -c "kafka-configs --bootstrap-server $ADMIN --alter --entity-type users --entity-name $u \
      --add-config-file /tmp/scram.properties >/dev/null; rc=\$?; rm -f /tmp/scram.properties; exit \$rc"
  done
  n=0
  set -f                         # "--topic *" means every topic, not the files in this folder
  while read -r args; do
    # shellcheck disable=SC2086  # the arguments are split on purpose
    docker exec broker kafka-acls $args >/dev/null < /dev/null
    n=$((n + 1))
  done < <("$PY" -m ops.kafka_admin acl-commands "$ADMIN")
  set +f
  mkdir -p logs
  echo "$stamp" > "$STAMP"
  echo "Kafka: $(echo $USERS | wc -w | tr -d ' ') users (SCRAM-SHA-512), $n ACL groups from ops/kafka_access.yaml"
}

case "${1:-}" in
  prepare) prepare ;;
  apply)   apply ;;
  check)   set -a; . "./$ENV_FILE"; set +a; KAFKA_CA_FILE="$PWD/certs/ca.crt" "$PY" -m ops.kafka_admin selfcheck ;;
  show)    docker exec broker kafka-acls --bootstrap-server "$ADMIN" --list ;;
  *)       sed -n '2,16p' "$0"; exit 2 ;;
esac
