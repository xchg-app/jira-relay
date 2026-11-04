---
tags: [memory/repo, index]
---

# What to read

| Task | File |
|---|---|
| Any work with Jira, xchg, docker or ssh | [gotchas.md](gotchas.md) |
| Change the note format, the field allowlist, masking | [../docs/design.md](../docs/design.md), then `jira_relay/render.py`, `tracker/diff.py`, `mask.py` |
| Poll cycle, window, errors | `jira_relay/tracker/poll.py` (docstring) |
| Config file, secrets layout, admin commands | `jira_relay/config.py` (docstring), `jira_relay/admin.py`, [../docs/design.md](../docs/design.md) "Configuration" |
| A new outbound type or auth scheme | `jira_relay/tracker/outbounds.py` (`Driver`), `jira_relay/jira.py` (`Auth`) |
| Agents → Jira: commands, permissions, signature, journal | [../client/skills/jira/SKILL.md](../client/skills/jira/SKILL.md), [../docs/design.md](../docs/design.md) "Agents → Jira", `jira_relay/gateway/` |
| Install, update, release | [../README.md](../README.md), `bin/release` |

## Code

- `jira_relay/jira.py` — REST v2 client and auth schemes (`pat`: Bearer); fetches the rest of the changelog and comments
- `jira_relay/issue.py` — raw JSON → normalized state (and fingerprint)
- `jira_relay/render.py` — texts of the snapshot and the change note
- `jira_relay/mask.py` — secret masking
- `jira_relay/config.py` — the config file model (sites, outbounds, users and their agents), secrets layout
- `jira_relay/admin.py` — admin commands: `setup`, `jira`, `outbound`, `user`
- `jira_relay/tracker/` — the tracker (Jira → outbounds): `ops.py` — operations (add/to/subtasks/rm/ls/watch/poll),
  `api.py` — facade for the gateway, `poll.py` — per-site poll cycle, `diff.py` — state diff, period and authors,
  `state.py` — sqlite (tracked, addresses, deliveries, meta), `outbounds.py` — outbound drivers (xchg), `hub.py` —
  `xchg post/sync`, hub connection, `address.py`, `attachments.py`
- `jira_relay/gateway/` — the agent gateway (`jira-relay agent`): `service.py` — entry point, `commands.py` — commands,
  sites, permissions, signature, user tokens, `journal.py` — the `actions` journal
- `jira_relay/cli.py` — command line over both parts
- Boundary: gateway → tracker only through `tracker/api.py` ([../docs/design.md](../docs/design.md) "Code")
- `client/` — what agents get: `bin/jira` (an ssh request to `jira-relay agent` on the service host; `bin/jira` at the
  repo root is a symlink to it) and `skills/jira/SKILL.md`. `harness/<name>/` — package manifests (Claude Code, Codex); `tools/package.sh`, `tools/publish.sh` and `.github/workflows/release.yml` build and publish them on a tag
- `docker/` — `entrypoint.sh` (`jira-relay setup`, then the command), `known_hosts` (default pinned hub host keys)
- `bin/release` — version bump, tag, GitHub Release via `gh`
