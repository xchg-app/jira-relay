# Changelog

Versions follow [SemVer](https://semver.org/): before 1.0, breaking changes bump the minor. Release: `bin/release X.Y.Z`
(README "Releases"): the "Unreleased" section becomes the version section, tag `vX.Y.Z`, GitHub Release.
Written for people and agents: what changed in behavior, not which files were touched.

## [Unreleased]

### Breaking
- Users register themselves with their Jira token instead of being added by the admin: the login is what Jira
  says the token's owner is, and the user gets a service token for the `jira` client. `admin`, `[users.*]`, agent
  `projects` and `watch_to` are gone from the config; `jira-relay user` is `ls | revoke LOGIN`. Every registered user
  may change any issue their Jira account can, and watch into any configured outbound; `jira watch` always takes an
  explicit address. The journal names every agent `<login>/<agent>`.
- Addresses have watchers: two users watching one issue into one address share it (one snapshot, one note per
  change), `jira unwatch` only leaves the watchers, the address goes with its last watcher. `ls`/`jira watching`
  list the watchers per address. Addresses added on the host take `--by LOGIN`; `jira-relay user claim LOGIN`
  adopts the ones without a watcher (needed once after the upgrade for the admin's old addresses).
- No service account: `jira-relay jira secret` is gone, polling and snapshots run with the watchers' own Jira
  tokens, grouped by watcher. A rejected token hands the issues to the next watcher; an issue none of them can read
  gets one "tracking paused" note (slug `<key>-paused`) and waits for a watcher to register again. Host commands
  that read Jira take `--by LOGIN` (`add` requires it).
- The gateway is an HTTPS service (`jira-relay serve`, port 8443 in the container): `POST /register` with a Jira
  token, `POST /agent` with the service token as `Authorization: Bearer`. `jira-relay agent` over ssh and
  `docker exec` are gone, users no longer need an account or the docker group on the host. The certificate is
  self-signed, generated at the first start; `jira-relay tls` prints its fingerprint for users to compare.

## [0.5.0] — 2026-11-05

### Breaking
- The agent client is published as packages generated on each release, like xchg: Claude Code
  (`xchg-app/jira-relay-claude-code`) and Codex CLI (`xchg-app/jira-relay-codex`). The marketplace in this repository is gone: reinstall from the package repository.

## [0.4.0] — 2026-11-04

The first public release.

### Tracker: Jira → outbounds
- Several named Jira Server/DC sites; issues are `site/KEY`, a bare key goes to the first site. Authentication per
  site is pluggable (`pat` today).
- Tracking of selected issues: a snapshot on confirmation, then one note per issue per change: status, resolution,
  priority, summary, assignee, versions, parent, description (diff), comments, links, subtasks, attachments (up to
  10 MB attached, larger ones marked). Subtasks can be picked up with their parent. Secret masking.
- Outbounds: xchg git hubs (shared or personal), several addresses per issue, failures isolated per address.

### Gateway: agents → Jira
- Claude Code plugin `jira-relay` with the `jira` skill and client: read, search, comment, create, transition,
  assign, edit, link, attach, `api` (allowlisted); watch issues into hubs. Changes only on the human's command,
  signed by the agent, journaled.
- A user is an account on the service host (ssh); each user puts their own Jira token per site (`jira token set`).
- `jira setup HOST` points the client at the service; `jira sites` shows sites, tokens and the agent's rights.

### Admin
- Configuration in the data volume (`/data/config/jira-relay.toml`, `/data/secrets`), managed with
  `jira-relay jira|outbound|user ...`, reread every cycle. Host values for the build and bot identity in `.env`.
