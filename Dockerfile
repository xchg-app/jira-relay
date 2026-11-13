# Small alpine image: on hosts with a slow external link large images and apt hang (see .claude-docs/gotchas.md).
FROM python:3.12-alpine

# apk mirror (e.g. a nexus alpine-proxy); empty — the default repositories
ARG ALPINE_MIRROR=
# The xchg client is a public repository, pinned to a commit: self-update is off in the container.
# v0.16.1: a failed commit to the hub is an error and the write is rolled back (before, "sent" with exit 0).
# The comment is on its own line:
# in a Dockerfile, text after an ARG value is not stripped.
ARG XCHG_REF=089ce5f001fc9448386420af0c9379d32fafde3b
# uid/gid of the service process and the owner of /data (compose passes APP_UID/APP_GID from .env)
ARG APP_UID=1000
ARG APP_GID=1000

# xchg is a bash script on top of git, awk, sed and coreutils; openssl makes the gateway's self-signed certificate
RUN release="v$(cut -d. -f1,2 /etc/alpine-release)" \
 && if [ -n "$ALPINE_MIRROR" ]; then \
      printf '%s/%s/main\n%s/%s/community\n' "$ALPINE_MIRROR" "$release" "$ALPINE_MIRROR" "$release" > /etc/apk/repositories; \
    fi \
 && apk add --no-cache git bash coreutils openssh-client openssl \
 && git clone -q https://github.com/xchg-app/xchg.git /opt/xchg \
 && git -C /opt/xchg checkout -q "$XCHG_REF" \
 && git config --system --add safe.directory '*'

COPY . /opt/jira-relay

# xchg takes the project from the git repository root: /opt/jira-relay must be a repository with that name.
RUN git init -q /opt/jira-relay \
 && chmod +x /opt/jira-relay/bin/jira-relay /opt/jira-relay/docker/entrypoint.sh \
 && mkdir -p /data/state /data/config /data/secrets \
 && chown -R "$APP_UID:$APP_GID" /data \
 && echo "jira-bot:x:$APP_UID:$APP_GID:jira-bot:/data:/sbin/nologin" >> /etc/passwd \
 && echo "jira-bot:x:$APP_GID:" >> /etc/group

ENV HOME=/data \
    PATH=/opt/jira-relay/bin:/opt/xchg/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JIRA_RELAY_DATA=/data \
    XCHG_CONF_DIR=/data/xchg \
    XCHG_NO_SELFUPDATE=1
# the write key is set per hub clone (core.sshCommand, tracker/hub.py): GIT_SSH_COMMAND would override it

USER ${APP_UID}:${APP_GID}
WORKDIR /opt/jira-relay
ENTRYPOINT ["/opt/jira-relay/docker/entrypoint.sh"]
CMD ["jira-relay", "run"]
