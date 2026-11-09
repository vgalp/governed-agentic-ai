#!/usr/bin/env bash
# Certificates for mutual TLS between the gateway and the tool servers (audit SC-6, ADR 030).
#
#   ./scripts/certs.sh          make certs/ once: a local CA, one server certificate for the tool
#                               servers (127.0.0.1, localhost) and one client certificate for the gateway
#   ./scripts/certs.sh renew    make them again (for example after a year, when they expire)
#   ./scripts/certs.sh kafka    add a certificate for the secured local Kafka broker (127.0.0.1,
#                               localhost, broker), signed by the same CA (ADR 031)
#
# With them, and GOVERNED_MTLS=on, the tool servers accept only connections that show the gateway's
# certificate: nothing else on the machine can call a tool directly and pass its own tenant or data
# classes. The private keys stay in certs/ (not committed). A deployment uses its own CA instead.

set -euo pipefail
cd "$(dirname "$0")/.."
DIR="certs"
DAYS=365

make_cert() {  # name, subject, extensions (run inside certs/)
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$2" -keyout "$1.key" -out "$1.csr" 2>/dev/null
  printf '%s\n' "$3" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days "$DAYS" \
    -extfile "$1.ext" -out "$1.crt" 2>/dev/null
  rm -f "$1.csr" "$1.ext" ca.srl
}
LEAF="basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer"

if [ "${1:-}" = "kafka" ]; then
  [ -f "$DIR/ca.crt" ] || bash scripts/certs.sh   # the CA first
  cd "$DIR"
  if [ -f kafka.pem ] && [ "${2:-}" != "renew" ]; then echo "certs/kafka.pem already exists"; exit 0; fi
  umask 077
  make_cert kafka "governed kafka broker" "$LEAF
subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:broker
extendedKeyUsage=serverAuth"
  # The broker reads one PEM file: its key (unencrypted PKCS#8), its certificate, then the CA.
  openssl pkcs8 -topk8 -nocrypt -in kafka.key -out kafka.p8
  cat kafka.p8 kafka.crt ca.crt > kafka.pem
  rm -f kafka.p8
  # Read by the broker container's own user; on Linux that is not your user, so the file is
  # readable by others on this machine (it never leaves certs/, which is not committed).
  chmod 644 kafka.pem ca.crt
  echo "Made certs/kafka.pem (broker key and certificate, valid $DAYS days)"
  exit 0
fi

if [ -f "$DIR/ca.crt" ] && [ "${1:-}" != "renew" ]; then
  echo "certs/ already exists (./scripts/certs.sh renew to make new ones)"
  exit 0
fi
mkdir -p "$DIR"
umask 077
cd "$DIR"
rm -f ./*.crt ./*.key ./*.csr ./*.srl ./*.ext

# The extensions strict certificate checking needs (Python 3.13 verifies with VERIFY_X509_STRICT).
# Written with -extfile rather than -addext, which the LibreSSL "openssl" on macOS may not have.
openssl req -newkey rsa:3072 -nodes -subj "/CN=Governed Agentic AI local CA" -keyout ca.key -out ca.csr 2>/dev/null
printf '%s\n' "basicConstraints=critical,CA:TRUE,pathlen:0" "keyUsage=critical,keyCertSign,cRLSign" \
  "subjectKeyIdentifier=hash" > ca.ext
openssl x509 -req -in ca.csr -signkey ca.key -days "$DAYS" -extfile ca.ext -out ca.crt 2>/dev/null
rm -f ca.csr ca.ext

make_cert server "governed tool servers" "$LEAF
subjectAltName=IP:127.0.0.1,DNS:localhost
extendedKeyUsage=serverAuth"
make_cert gateway "governed gateway" "$LEAF
extendedKeyUsage=clientAuth"
rm -f ca.srl
chmod 600 ./*.key
echo "Made certs/: ca.crt, server.crt/.key (tool servers), gateway.crt/.key (gateway), valid $DAYS days"
