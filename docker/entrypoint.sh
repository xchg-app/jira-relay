#!/bin/sh
# Container start: `jira-relay setup` connects the outbounds (xchg hubs). A failure is logged and does not stop the service: notes wait for the next sync.
set -u
jira-relay setup || echo "entrypoint: jira-relay setup failed" >&2
exec "$@"
