# jira-relay — design and note schema

jira-relay polls one or more Jira Server/DC sites and publishes changes of tracked issues to outbounds (xchg git hubs
today) as notes that people and coding agents read. It also lets agents act in Jira over ssh, each user with their own
Jira token.

## Overview

**Python 3.12, stdlib only** (urllib, sqlite3, difflib, json, tomllib). Runs as a docker compose service from a
`python:3.12-alpine` image together with the xchg client. One CLI, `jira-relay`, run in the container
(`docker compose exec jira-relay jira-relay …`):

```
jira-relay add DEMO-42 team:@backend alice [--with-subtasks] [--yes]  # snapshot to each address, sent on "yes"
jira-relay to DEMO-42 add|rm team:@backend:bob [--yes]  # addresses of a tracked issue; a new address gets a snapshot
jira-relay subtasks DEMO-42 on|off [--yes]              # pick up subtasks of a tracked issue; on: snapshots for existing ones
jira-relay rm DEMO-42 [--with-subtasks]
jira-relay ls
jira-relay poll [--dry-run]                             # one cycle; --dry-run prints notes instead of sending
jira-relay run                                          # the poll cycle, the container's process
jira-relay show DEMO-42                                 # render a snapshot without sending anything
jira-relay setup | jira … | outbound … | user …          # admin: connect outbounds, manage the config (README)
```

An issue is a ref `<site>/<KEY>` (`main/DEMO-42`); a bare key goes to the default site.

### Code: tracker, gateway and shared

Two parts in one process and image, with a boundary in the code, so that they can later be split between different
users and secrets without rewriting:

| Part | Where | What |
|---|---|---|
| shared | `jira_relay/{jira,issue,render,mask,config}.py` | Jira client and auth, issue normalization, snapshot and note texts, masking, the config file |
| tracker | `jira_relay/tracker/` | state (`state.py`), polling (`poll.py`, `diff.py`), outbounds (`outbounds.py`, `hub.py`, `address.py`), attachments, operations (`ops.py`) |
| gateway | `jira_relay/gateway/` | entry point `jira-relay agent` (`service.py`), commands and permissions (`commands.py`), journal (`journal.py`) |

The gateway reaches the tracker only through `tracker/api.py` (`watch`/`unwatch`/`subtasks`/`watching`): strings in,
text out; once the services are split, this becomes a call to a tracker command instead of an import. The tracker
knows nothing about the gateway, the shared part knows about no one; `tests/test_boundaries.py` checks this. The
gateway journal (`actions`) lives in the same `state.db`, but the tracker does not touch it. `cli.py` is the command
line over both parts, `admin.py` the admin commands (`setup`, `jira`, `outbound`, `user`).

### Runtime (docker)

| What | Where |
|---|---|
| process | service `jira-relay`: `jira-relay run`, a cycle every `interval` seconds of the config (300), `restart: unless-stopped`, `init: true`; the config is reread every cycle |
| code in the image | `/opt/jira-relay` + `git init`: xchg takes the project name from the repository root and signs notes `<bot login>/jira-relay` |
| xchg | `/opt/xchg`, pinned to a commit via `ARG XCHG_REF`, self-update disabled |
| data | volume `${JIRA_RELAY_VOLUME:-jira-relay_data}` → `/data`: `config/jira-relay.toml`, `secrets/`, `state/state.db` (sqlite), `xchg/` (bot config), `hub-<outbound>/` (hub clones) |
| secrets | files 0600 in `/data/secrets`, owned by `APP_UID`. Never in the repo, an outbound, logs, command output or `environment` |
| outbounds | at start `jira-relay setup` (entrypoint) connects each xchg outbound (`xchg hub add`, `xchg hub remote` when the address changes), sets `core.sshCommand` on the clone with its key and pinned host keys, and registers the `jira-relay` project there. `outbound key` connects right away |
| `.env` | `JIRA_RELAY_VOLUME`, `APP_UID`/`APP_GID` (process and `/data` owner), `ALPINE_MIRROR` (apk mirror), `BOT_NAME`/`BOT_EMAIL` (git author of hub commits) |
| log | `docker compose logs`, json-file 3 × 10 MB |

The cycle and the manual `add`/`to`/`subtasks`/`rm`/`poll` take `flock` on `/data/state/lock`. Each cycle starts with
`sync` of every outbound (`xchg sync` for a hub): it pushes notes written locally while a hub was unreachable (`post`
exits with code 4 in that case, and the note counts as sent).

### Configuration

One file, `/data/config/jira-relay.toml` (schema and example: the `jira_relay/config.py` docstring), written by
`jira-relay jira ls|add|rm|secret`, `outbound ls|add|rm|key|hostkey|owner`, `user ls|add|rm|outbounds|agent`
(`admin.py`) and readable by hand:

- `[jira.<site>]` `url`, `auth` — a named Jira site. `auth` picks an `Auth` class in `jira.py` (`pat`: Bearer token,
  user = `/myself.name`); another scheme (basic, OAuth, Cloud) is one more class. Secrets: `secrets/jira/<site>`
  (service secret: polling, snapshots, the admin's agents), `secrets/users/<login>/<site>` (a user's own token).
- `[outbound.<name>]` `type` + type options + optional `owner`. Drivers in `tracker/outbounds.py` (`setup`, `sync`,
  `post`); today `xchg` (`remote`, `login`; write key `secrets/outbound/<name>/ssh_key`, host keys
  `secrets/outbound/<name>/known_hosts` or the image's `docker/known_hosts`). An address is `<outbound>:<target>`, the
  target grammar belongs to the type. A personal outbound (`owner`) is used only by its owner; a shared one by the users
  that list it; the admin uses all.
- `admin`, `[users.<login>]` `outbounds`, `[users.<login>.agents.<name>]` `projects` (`site/PROJ`, `site/*`, `*`),
  `watch_to`.
- `timezone`, `max_attachment` (bytes, 10 MB), `interval` (s).

Environment: `JIRA_RELAY_DATA` (`/data`), `XCHG_*` for the xchg client, `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (from
`BOT_NAME`, `BOT_EMAIL`), `XCHG_PROJECT_REPO` (project card of `jira-relay` in hubs). Nothing installation-specific
lives in code, `compose.yaml` or the image.

### State (sqlite)

- `tracked(key, with_subtasks, parent_key, added_at)` — tracked issues (`key` is `<site>/<KEY>`: keys of different
  sites may collide). Subtasks found via `--with-subtasks` get here with `parent_key`.
- `addresses(key, address, added_by)` — addresses of top-level issues; a subtask is published to its parent's
  addresses. `added_by` is the agent that added the address via `jira watch` (empty: an admin command); informational.
- `deliveries(key, address, fingerprint, last_history_id, posted_at)` — the last delivered fingerprint per
  (issue, address) pair. The diff is computed for each address from its own fingerprint: an unreachable hub gets the
  note on the next cycle, the other addresses do not get a duplicate. An address without a delivery (a new subtask, an
  address added with `--no-snapshot` and the issue changed since) starts with a snapshot.
- `meta(last_poll:<site>)`.
- `actions` — the gateway journal (below).

### Poll cycle

Each site is polled by its own `Poller` with its service secret; one site failing does not stop the others.

1. JQL: `key in (…) AND updated >= "-Nm"`, then `parent in (…with subtasks…) AND updated >= "-Nm"`,
   `fields=<allowlist>`, `expand=changelog`. Keys in batches of 100. N is the minutes since the last poll plus a
   5-minute overlap. The window is relative because JQL evaluates absolute time in the account's time zone and to the
   minute. Duplicates are cut off by comparing fingerprints. The last poll time moves only if the whole cycle passed
   without errors. GET requests are retried up to three times on a network failure and on 429/502/503/504, so a
   transient DNS or proxy error does not make the cycle skip the window.
2. For each issue a fingerprint is built over the allowlist and compared with the saved one:
   - status (+ resolution), priority, summary, description, assignee, parent;
   - comments: id → author, time, text;
   - links (`issuelinks`) and subtasks;
   - `fixVersions`;
   - attachments: id → name, size, author.
3. No significant differences means no note; the fingerprint and `last_history_id` are updated silently. Noise
   (rank, sprint, worklog, watchers, labels, statuses of linked issues) is not compared.
4. Otherwise **one note per issue per cycle**. Authors and times of the changes come from the changelog and comments
   after `last_history_id`.
5. The fingerprint is saved **only after** a successful post; a failed send is retried next cycle. Delivery is "at
   least once": if the process dies between the post and the write, a duplicate is possible.
6. A new subtask of an issue tracked with `--with-subtasks` is tracked by itself and publishes a **snapshot** without
   confirmation: the flag is the consent.
7. An issue deleted or unavailable in Jira (404/403) gets one "unavailable, no longer tracked" note and is removed
   from `tracked`. JQL `key in (…)` with a nonexistent key fails as a whole (400), so such keys are checked one by
   one and the query is repeated without them.

### Attachments

Issue attachments go to the hub with the note (`xchg post --attach`): the file lands under `refs/xchg/att/<blob hash>`,
the message only has an `att:<hash>/<name>` reference in the `attachments:` line. A regular pull does not fetch such
files: a reader downloads only what they open (`xchg get`).

- **Size limit** `max_attachment` (10 MB). Anything larger is not downloaded at all: the note has the line
  "not sent to the hub: 42.1 MB is over the 10.0 MB limit, get it from Jira" with a link to the file in Jira.
- A failed download (network, permissions) still lets the note go, with the same mark and the reason.
- A snapshot carries all of the issue's attachments, a change note only the added ones.
- Files are downloaded into a temporary directory and deleted after sending; with several addresses the same file is
  uploaded to each hub separately (the reference is valid only in its own hub).
- **Attachments are not masked**: the note text goes through the masker, files go as is. A hub keeps them forever.

### Secret masking

Everything sent to an outbound, and every gateway reply, runs through `mask()`, which replaces secrets with
`«hidden: <kind>»`:
- `password|passwd|token|secret|api[_-]?key|client_secret … [:=] value` (plus the Russian keywords
  `пароль|токен|секрет`, since Jira content may be in Russian);
- `Bearer …`, `Basic …`;
- `scheme://user:pass@host` (password in a URL);
- `-----BEGIN … PRIVATE KEY----- … END`;
- known formats: `AKIA…`, `glpat-…`, `ghp_…`, `xox[bp]-…`, JWT `eyJ….….…`.

Long random strings without a keyword are left alone: there would be many false positives on commit hashes and ids.
The `warn_secrets` check in xchg stays as the second line of defense.

### Markup

Jira text (wiki markup) is passed as is: agents read it, and converting to markdown produces artifacts. The note's
markdown (headings, tables) is only the wrapping. The description and comments go into a code block (fence longer than
any run of backticks in the text): otherwise `# item` from a Jira numbered list would read as a note heading.

Times in notes are in the config's `timezone`; Jira returns UTC. Notes show bare keys and the site's URL.

## Note schema

The address is the full outbound address set on the issue, e.g.
`xchg post team:@backend <slug> --ref https://jira.example.com/browse/<KEY>`.
Forms: `team:@backend` (everyone on the project), `team:@backend:bob` (a person's agent on the project),
`team:carol` (a person), `team` = `team:all` (everyone in the hub). The outbound name is required and must be
configured. One note per issue per cycle, to each of its addresses.
Slug: `<key>-snapshot`, `<key>-unavailable` or `<key>-<first change kind>`, e.g. `demo-42-status`, `demo-42-comments`
(kinds in order: `status`, `resolution`, `priority`, `summary`, `assignee`, `fix-versions`, `parent`, `description`,
`comments`, `links`, `subtasks`, `attachments`).
The first line of the body (`# …`) is shown in `xchg inbox`, so it carries the key and the gist of the change.

Issue data (summary, names, texts) comes from Jira as is; below it is shown as placeholders. Names are
`<display name> (<login>)`, times are `YYYY-MM-DD HH:MM` in the configured `timezone`, an empty value is `—`.

### Snapshot (on `add` after confirmation, on `jira watch`, for a new subtask or address)

````markdown
# DEMO-42 · snapshot: <issue summary>

| | |
|---|---|
| Link | https://jira.example.com/browse/DEMO-42 |
| Type / status | Story / In Progress (resolution: —) |
| Priority | Medium |
| Assignee | Alice Smith (alice) |
| Reporter | Bob Jones (bob) |
| Fix versions | 2.4, 2.5 |
| Parent | DEMO-40 «…» — In Progress |
| Created / updated | 2026-08-20 11:50 / 2026-09-11 10:05 (UTC) |
| Tracking | with subtasks |

## Description
```
<in full, wiki markup as is>
```

## Subtasks
- DEMO-43 «…» — To Do

## Links
- blocks DEMO-44 «…» — Review
- is blocked by DEMO-12 «…» — Done

## Attachments
- spec.pdf — 120.0 KB, Bob Jones (bob), 2026-08-21 10:00 — attached to the note; in Jira: https://jira.example.com/secure/attachment/…
- dump.zip — 42.1 MB, Bob Jones (bob), 2026-08-21 10:05 — not sent to the hub: 42.1 MB is over the 10.0 MB limit, get it from Jira: https://jira.example.com/secure/attachment/…

## Comments (N)

### Alice Smith (alice) · 2026-08-22 14:03
```
<text in full>
```

### Bob Jones (bob) · 2026-08-23 09:10 (edited 2026-08-23 09:40)
```
<text in full>
```
````

The `Parent` row and the `Subtasks`, `Links`, `Attachments` sections are omitted when empty; an empty description
is `—`. `Tracking` is `with subtasks` or `without subtasks`.

### Change (one per issue per cycle, empty sections omitted)

````markdown
# DEMO-42 · status In Progress → Review, summary, assignee → Bob Jones, description, +1 comment, comments edited: 1, comments deleted: 1, links, subtasks, attachments

DEMO-42 «<summary>» · https://jira.example.com/browse/DEMO-42
Changes 2026-09-11 10:05–10:20 (UTC) · Alice Smith, Bob Jones

## Fields
| Field | Before | After |
|---|---|---|
| Status | In Progress | Review |
| Summary | old | new |
| Assignee | Alice Smith (alice) | Bob Jones (bob) |

## Description
```diff
@@ -12,3 +12,4 @@
 context
-old line
+new line
```

## Comments

### + Alice Smith (alice) · 2026-09-11 10:12
```
<text in full>
```

### ~ Bob Jones (bob) · 2026-09-11 10:18 (comment by Bob Jones (bob), 2026-09-01 15:00)
```diff
<diff of the changed part>
```

### − deleted comment by Carol White (carol), 2026-08-30 12:00

## Links
- + blocks DEMO-44 «…» — To Do
- − relates to DEMO-30 «…»

## Subtasks
- + DEMO-45 «…» — To Do

## Attachments
- + log.txt — 3.0 KB, Alice Smith (alice), 2026-09-11 10:12 — attached to the note; in Jira: https://jira.example.com/secure/attachment/…
- − old.log
````

The title lists the changes in this order, comma-separated:
`status <old> → <new>`, `resolution <new>` (or `resolution cleared`), `priority <old> → <new>`,
`summary`, `assignee → <display name>`, `fix versions`, `parent`, `description`, `+N comment`/`+N comments`,
`comments edited: N`, `comments deleted: N`, `links`, `subtasks`, `attachments`.
The `Changes …` line appears when the period is known; a period within one day is `2026-09-11 10:05–10:20`,
across days `2026-09-10 23:50 – 2026-09-11 00:10`, a single moment is one time.
A new subtask gets its snapshot as a separate note.

Description: if it was empty, it is given in full; otherwise a unified diff with three lines of context.
An edited comment is a diff too, a new one is in full.

### Issue unavailable

````markdown
# DEMO-42 · issue unavailable in Jira, no longer tracked

Jira returns 404 for https://jira.example.com/browse/DEMO-42: the issue was deleted, moved to another project, or the service account has no access to it. Its changes are no longer published; to track it again, run `jira-relay add DEMO-42 <@project>` on the service host.
````

## Agents → Jira

Coding agents (Claude Code on a user's PC, in any repository) act in Jira **through this service**: the Jira tokens
stay on the service host, nothing is exposed from it except ssh, and no MCP server is involved.

- An agent may do almost anything: read, comment, create issues, change statuses and fields, link, attach.
- Reading is free; changes happen only on the human's command or consent in the agent's session (an agent rule in the
  skill, backed by Claude Code's permission prompt; the service cannot check it).
- Actions come from the user's own Jira account and are **signed by the agent**.
- Agents put issues on tracking themselves (`watch`) into the user's outbounds. The snapshot goes out right away: the
  human's command to the agent is the confirmation. Any of the user's agents can remove tracking.

### Scheme

```
agent (PC) ── jira --as backend comment DEMO-42 <<'TXT' … ──────────────────┐
                                                                            │ JSON on stdin
      ssh HOST 'docker exec -i jira-relay-jira-relay-1                      │
                jira-relay agent --user "$(id -un)"'                        │ JSON on stdout
service (container with the secrets) ── REST v2 ── Jira site(s) ────────────┘
```

- The client `client/bin/jira` (packaged for Claude Code and Codex from `harness/`) is stdlib only. It builds the
  request as JSON (`{"as", "argv", "text", "files"}`) and passes it via stdin, so text with quotes, non-ASCII and
  newlines never goes through the remote shell. It prints the reply briefly (a link, the key of a created issue) or in
  full with `--json`. The service parses the commands: new commands do not require updating the client.
- Host: `jira setup HOST [--container NAME]` → `~/.config/jira-relay/client.conf`, or `JIRA_RELAY_HOST` /
  `JIRA_RELAY_CONTAINER`. ssh-agent socket: `JIRA_SSH_AUTH_SOCK`, else `~/.ssh/agent.sock` if it exists, else plain
  ssh; a dead agent gives a clear error instead of a hanging ssh.
- Every reply carries the service version; a client older than the service asks to update the plugin (the skill next
  to it would be outdated too).
- `jira-relay agent` does not take the poll cycle `flock`: writing to Jira does not interfere with publishing. Tracked
  issues get a regular change note after an agent's actions; that is intended.

### Who the user is

**A user is an account on the service host.** Everyone connects over ssh with their own login (the `docker` group is
needed); the client runs `docker exec -i … jira-relay agent --user "$(id -un)"`, the substitution happens on the host,
and the request JSON has no user. An agent writing the user itself could get it wrong; the account cannot be mixed up
this way. A call can be forged only deliberately: anyone in the `docker` group can reach the service secrets in the
container anyway. The separation protects against agents' mistakes (an action on behalf of the wrong person), not
against malice.

- `admin` in the config uses the service secret of each site; `token` commands are refused for them.
- Other users are `[users.<login>]`; the login is also the Jira login. Not on the list means refusal of everything.
  They put their own token per site: `jira token set --jira SITE` (stdin; from a terminal it asks without echo). The
  service checks it via `/rest/api/2/myself` (the Jira login must match) and stores it in
  `secrets/users/<login>/<site>`, 0600; it never appears in the journal, replies or errors. `token status`, `token rm`.
  Without a token for a site only `token` and `sites` work there.
- Reading and changes use the user's token (visibility and authorship in Jira are theirs). **Polling and snapshots**
  use the service secret: there is one cycle.

### Who the agent is

`--as <name>` is the agent's project name, anywhere in the command. Agents have no keys of their own: all of a
person's agents use their ssh key, so an agent key would prove nothing; the name is for the signature and the journal.
Any agent may read and watch without registration. Changes are allowed only to agents in
`[users.<login>.agents.<name>]` and only in their `projects` (`main/DEMO`, `main/*`, `*`); `watch_to` is the default
`watch` address. This protects against agent mistakes, not against an attacker.

Sites: an issue is `<site>/<KEY>` or a bare key. A bare key (and a command without an issue: `search`, `api`, `create`
with a bare project, `token`) goes to `--jira NAME` (anywhere in argv), else to the agent's site if all its projects
name one site, else to the first configured site. `sites` lists the sites, the user's token per site, what the agent
may change and where it may watch to.

### Commands

```
jira --as A sites                                  # sites, tokens, rights: what to know before working
jira --as A get DEMO-42 [--json]                   # reading: no confirmation
jira --as A search 'project = DEMO AND …' [--limit 50]
jira --as A transitions DEMO-42
jira --as A comment DEMO-42 < text                 # changes: only on the human's command
jira --as A create DEMO --type Task --summary … [--parent DEMO-42] [--assignee login] [--priority P] [--label L] < description
jira --as A transition DEMO-42 'Review' [--comment < text]   # by transition or status name, not by id
jira --as A assign DEMO-42 login|-
jira --as A edit DEMO-42 priority=High labels+=x customfield_1:=<json> …
jira --as A link DEMO-42 blocks DEMO-43            # type name or its phrase; one site
jira --as A attach DEMO-42 file.png                # up to max_attachment; the file travels in the JSON (base64)
jira --as A api GET|POST|PUT /rest/api/2/… [< json] [--jira SITE]
jira --as A watch DEMO-42 [address…] [--with-subtasks] [--dry-run] [--no-snapshot]
jira --as A unwatch DEMO-42 [address] | subtasks DEMO-42 on|off | watching
jira --as A token set|status|rm [--jira SITE]
jira --as A log [--since 7d] [--all]               # action journal
```

### Tracking

`watch`/`unwatch`/`subtasks`/`watching` are the same tracker operations as the admin's `add`/`to`/`subtasks`/`rm`/`ls`
(`tracker/ops.py`); the gateway calls them through `tracker/api.py` under the cycle lock (the command waits for the
current cycle). Applied immediately: the snapshot at the moment of the command, changes from the next cycle.

- `watch KEY [address…]`: without an address, the agent's `watch_to`. A new issue: like `add --yes`; an already
  tracked one: the missing addresses (snapshot only there) and, with `--with-subtasks`, `subtasks on`. `--dry-run`
  shows the snapshots, `--no-snapshot` skips them. The user needs their own token for the site.
- `unwatch KEY [address]`: remove the address; the last address or no address untracks the issue with its subtasks.
  A non-admin user only touches addresses in their own outbounds.
- Outbounds only from the config, and only those the user may use (own personal ones, shared ones listed for them; the
  admin all). A new outbound is connected by the admin (`jira-relay outbound add/key`); an agent gets a refusal with the
  list of allowed outbounds.
- `addresses.added_by` records the agent (`backend` for the admin's, `<login>/backend` for others'); shown in
  `watching` and `ls`, grants no permissions.

### Signature

Wherever there is text, the service appends `\n\n_— agent <name>_` (Jira Server wiki markup): a comment, the
description of a created issue, a transition comment; in `api`, the `body` field of a comment request. Actions without
text (status without a comment, assignee, fields, links, attachments) have no signature, only the journal.

### Restrictions

- `api` only for `/rest/api/2/` and `/rest/agile/1.0/` (tokens `/rest/pat/` and sessions `/rest/auth/` are excluded).
  `GET`: anything under them. `POST`/`PUT`: an **allowlist**, not a blocklist: `issue…`, `issueLink`, `search`,
  `version…`, `component…`, `filter…`; agile: `issue`, `sprint`, `backlog`, `epic`. Administration, permissions,
  schemes, users and groups are cut off by this. `DELETE` is not allowed at all: deleting is done by people in Jira.
- The target project in `api` is detected by the issue key in the path, `fields.project.key`,
  `inwardIssue`/`outwardIssue`, the `project` of a version. Undetectable (a numeric id in the path) means refusal,
  except for agents with `"*"`.
- An agent may read everything its user can see on every site, but change only its own projects.
- Replies and errors go through `mask()`: a token does not leak even into the output to the agent.

### Journal

Table `actions` in `state.db`: time, agent, action, issue (`<site>/<KEY>`), a short request (text: the first 200
characters), result (a link or a Jira error). Changes that reached Jira are logged, including failed ones; service
refusals before Jira and reads are not. `jira log` shows the agent's own actions, `--all` all of them.

### How the agent learns about it

The instruction is the skill `client/skills/jira/SKILL.md`, shipped with the client in the `jira-relay` package for
the agent's harness; it is the only instruction for all agents, so agent commands change together with it. Each session sees only
the skill's description; the text loads when Jira comes up. Nothing goes into the agents' repositories.

A recommended safeguard: allow only reading commands (`jira get|search|transitions|sites|watching|log|--help`) without
prompting in Claude Code settings, so Claude Code asks the human about everything that writes. That is why `--as` can
go anywhere in the command: permissions are matched by its beginning.

## Possible next steps

- Other outbound types (email, Telegram, chat webhooks): a `Driver` in `tracker/outbounds.py` with its own target
  grammar.
- Other auth schemes (basic, OAuth, Jira Cloud): one more `Auth` class in `jira.py`.
- Monitoring: cycle health and outbound backlog exposed for an external check.
- Splitting the tracker and the gateway into separate services with separate secrets (the code boundary is already in
  place).
