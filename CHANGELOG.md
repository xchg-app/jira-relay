# Changelog

Versions follow [SemVer](https://semver.org/): before 1.0, breaking changes bump the minor. Release: `bin/release X.Y.Z`
(README "Releases"): the "Unreleased" section becomes the version section, tag `vX.Y.Z`, GitHub Release.
Written for people and agents: what changed in behavior, not which files were touched.

## [Unreleased]

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
