#!/usr/bin/env bash
# A local identity provider for trying sign-in (ADR 028): Keycloak on http://127.0.0.1:8180 with
#   realm   governed-demo
#   client  governed-ui     public, authorization code with PKCE, for the pages on port 8000
#   groups  clinicians, nurses, front-desk, compliance   (mapped to roles by the profile's sign_in.roles)
#   users   dr.lee, nurse.kim, desk.ortiz, audit.chen    one per group
#
#   ./scripts/keycloak_setup.sh          start Keycloak and set it up (once; later runs only start it)
#   ./scripts/keycloak_setup.sh users    print the test users and their passwords again
#   ./scripts/keycloak_setup.sh reset    delete the realm and set it up again
#
# The admin password and the test users' passwords are made here, at random, and kept in
# .keycloak.env (not committed). They are test accounts for this machine only.
#
# Then start the pipeline with sign-in on:
#   GOVERNED_SIGN_IN=oidc PROFILE=clinic-assistant ./scripts/dev.sh start

set -euo pipefail
cd "$(dirname "$0")/.."

SECRETS=".keycloak.env"
REALM="governed-demo"
KC_URL="http://127.0.0.1:8180"
USERS=(  # username | first | last | group
  "dr.lee|Dana|Lee|clinicians"
  "nurse.kim|Sam|Kim|nurses"
  "desk.ortiz|Alex|Ortiz|front-desk"
  "audit.chen|Jo|Chen|compliance"
)

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }
pw()   { openssl rand -base64 18 | tr -d '/+=' | cut -c1-20; }
var()  { printf 'PW_%s' "$(echo "$1" | tr '.-' '__' | tr '[:lower:]' '[:upper:]')"; }

if [ ! -f "$SECRETS" ]; then
  umask 077
  {
    echo "# Test passwords for the local Keycloak (scripts/keycloak_setup.sh). This machine only."
    echo "KEYCLOAK_ADMIN_PASSWORD=$(pw)"
    for u in "${USERS[@]}"; do echo "$(var "${u%%|*}")=$(pw)"; done
  } > "$SECRETS"
fi
set -a; . "./$SECRETS"; set +a

print_users() {
  say "Test users (realm $REALM, sign in at http://127.0.0.1:8000):"
  for u in "${USERS[@]}"; do
    IFS='|' read -r name _ _ group <<< "$u"
    v="$(var "$name")"
    printf '  %-12s %-22s group %s\n' "$name" "${!v}" "$group"
  done
}

if [ "${1:-}" = "users" ]; then print_users; exit 0; fi

say "Starting Keycloak"
docker compose --profile auth up -d keycloak >/dev/null
for _ in $(seq 90); do
  curl -sf "$KC_URL/realms/master" >/dev/null 2>&1 && break
  sleep 2
done
curl -sf "$KC_URL/realms/master" >/dev/null 2>&1 || fail "Keycloak did not start: docker compose logs keycloak"
ok "Keycloak on $KC_URL"

KC=(docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh)
"${KC[@]}" config credentials --server http://localhost:8080 --realm master --user admin \
  --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null || fail "Admin sign-in failed. If Keycloak was set up before $SECRETS existed, run: docker compose --profile auth down -v"

if [ "${1:-}" = "reset" ]; then
  "${KC[@]}" delete "realms/$REALM" >/dev/null 2>&1 || true
  ok "Deleted realm $REALM"
fi

if "${KC[@]}" get "realms/$REALM" >/dev/null 2>&1; then
  ok "Realm $REALM is already set up"
  print_users
  exit 0
fi

say "Setting up realm $REALM"
"${KC[@]}" create realms -s realm="$REALM" -s enabled=true -s displayName="Governed Agentic AI (local test)" >/dev/null
ok "Realm"

CID=$("${KC[@]}" create clients -r "$REALM" -i \
  -s clientId=governed-ui -s name="Governed Agentic AI pages" -s publicClient=true \
  -s standardFlowEnabled=true -s directAccessGrantsEnabled=false -s implicitFlowEnabled=false \
  -s 'redirectUris=["http://127.0.0.1:8000/*","http://localhost:8000/*"]' \
  -s 'webOrigins=["http://127.0.0.1:8000","http://localhost:8000"]' \
  -s 'attributes."pkce.code.challenge.method"=S256' \
  -s 'attributes."post.logout.redirect.uris"=http://127.0.0.1:8000/*##http://localhost:8000/*')
"${KC[@]}" create "clients/$CID/protocol-mappers/models" -r "$REALM" \
  -s name=groups -s protocol=openid-connect -s protocolMapper=oidc-group-membership-mapper \
  -s 'config."claim.name"=groups' -s 'config."full.path"=false' \
  -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=true' -s 'config."userinfo.token.claim"=true' >/dev/null
"${KC[@]}" create "clients/$CID/protocol-mappers/models" -r "$REALM" \
  -s name=audience -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.custom.audience"=governed-api' -s 'config."access.token.claim"=true' >/dev/null
ok "Client governed-ui (public, PKCE), with the groups claim and audience governed-api"

GIDS=""   # "group=id " pairs (no associative arrays: macOS ships bash 3.2)
for g in clinicians nurses front-desk compliance; do
  GIDS="$GIDS$g=$("${KC[@]}" create groups -r "$REALM" -i -s name="$g") "
done
gid() { for pair in $GIDS; do [ "${pair%%=*}" = "$1" ] && echo "${pair#*=}"; done; }
ok "Groups: clinicians, nurses, front-desk, compliance"

for u in "${USERS[@]}"; do
  IFS='|' read -r name first last group <<< "$u"
  v="$(var "$name")"
  UID_=$("${KC[@]}" create users -r "$REALM" -i -s username="$name" -s enabled=true \
    -s firstName="$first" -s lastName="$last" -s email="$name@example.test" -s emailVerified=true)
  "${KC[@]}" set-password -r "$REALM" --userid "$UID_" --new-password "${!v}" >/dev/null
  G="$(gid "$group")"
  "${KC[@]}" update "users/$UID_/groups/$G" -r "$REALM" -s realm="$REALM" -s userId="$UID_" -s groupId="$G" -n >/dev/null
done
ok "Users: ${#USERS[@]}"

print_users
say "Next: GOVERNED_SIGN_IN=oidc PROFILE=clinic-assistant ./scripts/dev.sh start"
echo "  Two people at once (asker and approver): use a second browser, or a private window."
