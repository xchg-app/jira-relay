# CLAUDE.md — jira-relay

A self-hosted service (docker compose) with two halves: the **tracker** polls one or more named Jira Server/DC sites and
publishes changes of tracked issues to outbounds (xchg hubs today); the **gateway** takes coding agents' actions in
Jira over HTTPS (the `jira` client in `client/`, packaged for Claude Code and Codex from `harness/`). Users register
themselves with their own Jira token; there is no service account, polling runs with the watchers' tokens.
Everything about an installation lives in its data volume (`/data/config/jira-relay.toml`, `/data/secrets`,
`/data/state`), managed by `jira-relay jira|outbound|user`; nothing installation-specific goes into code,
`compose.yaml` or the image.
More in [`.claude-docs/`](.claude-docs/index.md).

## Documentation index
- [.claude-docs/index.md](.claude-docs/index.md) — what to read for a task, code map
- [.claude-docs/gotchas.md](.claude-docs/gotchas.md) — non-obvious pitfalls (Jira, xchg, docker)
- [docs/design.md](docs/design.md) — architecture, config, state, note schema, agent gateway
- [client/skills/jira/SKILL.md](client/skills/jira/SKILL.md) — instructions for agents: Jira via `jira`
- [README.md](README.md) — install, commands, what lives where

## Commands
- tests: `python3 -m unittest discover -s tests -t .`
- release: `bin/release X.Y.Z` (README "Releases"; the tag's workflow publishes the packages); record what changed under "Unreleased" in `CHANGELOG.md` in the same commit
- snapshot without config or token: `./bin/jira-relay show --raw <issue.json>`
- on a service host: `docker compose exec tracker jira-relay <cmd>`

## Boundaries
### MUST
- Everything sent to an outbound goes through `mask()`.
- Change the note schema together with `docs/design.md`: agents reading the notes rely on it.
- The gateway (`jira_relay/gateway/`) reaches the tracker only through `jira_relay/tracker/api.py` (checked by `tests/test_boundaries.py`).
- Publish only through outbound drivers (`jira_relay/tracker/outbounds.py`; xchg via `tracker/hub.py`).
- Change agent commands together with `client/skills/jira/SKILL.md`: it is the only instruction for all agents.
- Python 3.12 stdlib only, English only (code, notes, docs).
### MUST NOT
- A Jira token or any secret in the repository, an outbound, logs or command output. Live Jira calls run on the service host.
- Take the agent's user from the request JSON: only from the service token (`gateway/auth.py`), which registration
  with the user's own Jira token issued.
- Send a snapshot to an outbound without the human's confirmation: an admin's "yes" or their command to an agent (`jira watch`).
- Installation data (hosts, logins, project keys of a real company) in code, tests or docs: use `jira.example.com`, `DEMO`, `alice`.

## Workflow
- Repository `github.com/xchg-app/jira-relay`, branch `main`; services run release tags `vX.Y.Z`.
- One-line commits, with the issue number in parentheses if there is one.
