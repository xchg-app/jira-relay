#!/bin/sh
# Container start. The tracker (`jira-relay run`) connects the outbounds first: a failure is logged and does not stop
# the service, notes wait for the next sync. The gateway (`jira-relay serve`) needs a TLS certificate: a self-signed
# one is generated once into the data volume; `jira-relay tls` prints its fingerprint for the users.
set -u
tls="${JIRA_RELAY_DATA:-/data}/secrets/tls"
case "$*" in
  *serve*)
    if [ ! -s "$tls/cert.pem" ] || [ ! -s "$tls/key.pem" ]; then
      mkdir -p "$tls" && chmod 700 "$tls" \
      && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
           -subj "/CN=jira-relay" -keyout "$tls/key.pem.tmp" -out "$tls/cert.pem.tmp" 2>/dev/null \
      && chmod 600 "$tls/key.pem.tmp" && mv "$tls/key.pem.tmp" "$tls/key.pem" && mv "$tls/cert.pem.tmp" "$tls/cert.pem" \
      || echo "entrypoint: could not generate the TLS certificate in $tls" >&2
    fi ;;
  *run*)
    jira-relay setup || echo "entrypoint: jira-relay setup failed" >&2 ;;
esac
exec "$@"
