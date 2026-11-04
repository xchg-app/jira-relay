---
tags: [memory/repo, gotcha]
---

# Gotchas

## Jira Server/DC

- **JQL `key in (…)` with a single nonexistent key fails entirely**: 400
  `An issue with key 'X' does not exist for field 'key'`. `poll` checks such keys one by one
  (`_drop_unavailable`) and repeats the query without them.
- **Absolute time in JQL is in the account's time zone, with minute precision.** That is why the poll
  window is relative: `updated >= "-Nm"`. Dates in JSON come in UTC (`+0000`).
- **Search may truncate the changelog** (`total > len(histories)`): then the issue is re-read
  via `GET /issue/{key}?expand=changelog`, which returns the full history.
- **Subtasks are the standard `parent` field.** Jira also has `Epic Link` and `Parent Link` (Advanced Roadmaps)
  custom fields; "with subtasks" does not cover them.
- **Descriptions and comments come with `\r\n`.** Hubs normalize to LF (`.gitattributes eol=lf`), git warns
  noisily, and description diffs would be full of `\r`. `issue.text()` strips CR during normalization;
  `diff` compares texts without CR so that fingerprints stored with `\r\n` do not produce false changes.
- Logins can be capitalized (`Alice`); some users have a `key` like `JIRAUSER11505`. Compare logins case-insensitively.
- **Transient network errors happen** (DNS "Try again", 502/503 from a proxy): `Jira._get` retries up to three
  times (`RETRIES`, `RETRY_DELAY`) on a network error and on 429/502/503/504. GET requests only.

## Agents → Jira (`jira_relay/gateway/`, `plugin/bin/jira`)

- **The link direction in `POST /rest/api/2/issueLink` is reversed relative to the field names**: the link source
  (the one Jira shows the outward phrase on, "blocks") is `inwardIssue`. "A blocks B" = `inwardIssue: A, outwardIssue: B`.
  That is why `link` re-reads the issue after creating it and prints the link as Jira sees it.
- **A transition comment is a separate request**, not `update.comment` in the transition: for a transition without
  a screen Jira answers "Field 'comment' cannot be set".
- **Writes are not retried** (`Jira.write`): a timeout does not mean Jira did not perform it. Only GETs are retried.
- `X-Atlassian-Token: no-check` is mandatory for attachment uploads, otherwise 403 (XSRF).
- `jira-relay agent` does not take the cycle's `flock`: it writes to Jira, bypassing the outbounds. The journal is in
  the same sqlite, hence `sqlite3.connect(timeout=30)`.
- Agents' changes to tracked issues come back to the hub as a regular change note (author: the user's Jira account,
  with the agent's signature in the text). This is expected.
- **The user is the account on the service host, not the request JSON.** The client passes `--user "$(id -un)"`, the
  substitution runs on the host. Do not move it into the JSON: the agent would write it itself and could get it wrong,
  while the account cannot be mixed up. A call can be forged deliberately; there is no forced-command key (a user's
  ssh key opens a shell on the host anyway).
- Everyone in the host's `docker` group can reach the service secrets in the container. User separation protects
  against agents' mistakes, not against people with docker.
- The client waits for stdin no longer than a second: agents sometimes have stdin open and empty, without this the
  command would hang.

## xchg

- **xchg takes the sender's project from the name of the git repository root.** `tracker/hub.py` runs xchg
  with `cwd` = the code root. In the image the code lives in `/opt/jira-relay`, and `git init` is done there: without
  it notes would go out signed with the bot login and no project. The root is computed from the file (`parents[2]` in
  `tracker/hub.py`): fix it if the module moves to another level.
- **`safe.directory '*'` in the image is mandatory.** The code in the image belongs to root, while the process runs
  as `APP_UID`: without it git refuses to work ("dubious ownership"), the current project silently becomes empty, and
  the project signature disappears.
- **Unsent to the hub.** If the hub is unreachable, `post` still writes the note to the local clone and exits with
  code **4**. `hub.post` treats this as success: the fingerprint is saved, otherwise the next cycle would send a
  duplicate. `xchg sync` pushes such commits later (code 4 while something remains); every cycle starts with it.
- **Attachments** live in the hub under `refs/xchg/att/<blob hash>`, outside branches; a message refers to them with
  an `attachments:` line. The hosting must accept refs outside branches and tags (GitLab, GitHub and xchg.app do).
  The client sets no size limit itself; jira-relay's limit is `max_attachment` in the config, see `attachments.py`.
- **The client normalizes attachment names itself** (`att_name`): letters of any alphabet are kept, spaces and
  ASCII punctuation collapse into a single `_`. Do not transliterate or rename on the jira-relay side.
- **On xchg.app an attachment cannot be deleted from the hub by push**: pre-receive refuses it, although the client
  docs suggest `git push origin :refs/xchg/att/<hash>` (that is for hubs with a softer hook). Whatever goes to the hub
  stays there forever: check size and content before sending, not after.
- **The masker checks text only.** An attachment goes to the hub as is, and the hub keeps it forever,
  hence the limit and the refusal to download everything.
- **xchg self-updates (`git pull` of its own clone) on every sync/post.** The image has
  `XCHG_NO_SELFUPDATE=1`; the client version is `ARG XCHG_REF` in the Dockerfile. If the hub contract is bumped,
  the client in the image stops writing until `XCHG_REF` is updated.
- The bot's xchg config is inside the volume (`XCHG_CONF_DIR=/data/xchg`); `jira-relay setup` runs
  `xchg hub add … --login <login>` for each xchg outbound of the config.
- **xchg.app assigns the login by key itself.** A bot key added to a user account `alice` gets the login
  `alice.jira-bot`; registering under another login is rejected ("you are alice.jira-bot here, but you are registering
  jira-bot"). That is why each xchg outbound has its own `login`, and `hub.connect` reconnects the hub when the login
  changes (`xchg hub rm` + `hub add`: the clone stays, the config changes).

## Docker and ssh

- **Each hub has its own write key**, set on the hub clone via `core.sshCommand`
  (`ssh -i <key> -o IdentitiesOnly=yes -o UserKnownHostsFile=<known_hosts>`). Do not set `GIT_SSH_COMMAND` in the
  image: the environment variable overrides `core.sshCommand` and would force one key on all hubs. For
  `xchg hub add`/`hub remote` (no clone yet, or it is moving) `hub.connect` sets `GIT_SSH_COMMAND` for that call only.
  Without `IdentitiesOnly` ssh may offer another key first and the hub may accept it under a different identity.
- **Hub host keys are pinned.** The image's `docker/known_hosts` has xchg.app and github.com; for any other hub
  host: `ssh-keyscan HOST > kh`, verify the fingerprint with the hub owner, then
  `jira-relay outbound hostkey NAME < kh` (stored as `secrets/outbound/NAME/known_hosts`, used instead of the default).
- **The image has an `/etc/passwd` entry for `APP_UID`**: without it OpenSSH fails with "No user exists for uid".
  If you change `APP_UID`/`APP_GID` in `.env`, rebuild the image; files in an existing volume keep the old owner.
- **No global `git user.name/email`**: the author of hub commits is set via `GIT_AUTHOR_*` / `GIT_COMMITTER_*` in
  `compose.yaml` (from `BOT_NAME`, `BOT_EMAIL` in `.env`).
- **Secrets are files in the volume** (`/data/secrets`, 0600, owned by `APP_UID`). They never go into `environment`:
  it would be visible in `docker inspect`.
- **The volume name comes from `.env`** (`JIRA_RELAY_VOLUME`, default `jira-relay_data`). Changing it, or losing
  `.env` when it set a custom name, makes compose start on an empty volume: no config, no state, hubs connected anew.
- `init: true`: otherwise python would be PID 1 and ignore SIGTERM, and `docker stop` would wait 10 s.
- The cycle and manual `add`/`rm`/`poll` synchronize via `fcntl.flock` on `/data/state/lock`: concurrent git
  operations in a hub clone conflict (`index.lock`).
- **Slow external link on the host**: large images and package downloads may stall. The image is small
  (`python:3.12-alpine`), and `ALPINE_MIRROR` in `.env` points `apk` to a local mirror.
- **An interrupted `ssh HOST '…'` may keep running on the host.** Check `ps` there before repeating, otherwise two
  deployment chains run in parallel. A `docker run --rm` whose client was killed leaves the container running.
- In `ssh HOST '…'` do not put `set -e` before commands that are expected to fail in a check: it aborts everything,
  cleanup included.
- The client's ssh runs with `BatchMode=yes`: a missing or locked key fails fast instead of prompting inside an agent.

## Development

- With `core.autocrlf=true` (common on Windows/WSL) scripts would check out with CRLF and the shebang/entrypoint in
  the image would break; `.gitattributes` (`eol=lf`) prevents it.
- Do not put a Jira token on a development machine: run live Jira requests on the service host.
