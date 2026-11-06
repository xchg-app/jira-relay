---
name: jira
description: Jira (Server/DC) through a jira-relay service — read an issue, search by JQL, comment, create an issue or subtask, change status, assignee, fields, link, attach a file, watch an issue into an xchg hub. Use when your work mentions a Jira issue key (ABC-123) or your human asks for something in Jira.
---
# Jira through jira-relay

You read and change Jira through a jira-relay service over HTTPS. You have no Jira token and need none: your human
registered with the service using their Jira token, and the client carries the service token it got. Everything you
do in Jira is done from your human's Jira account.

Everything goes through the `jira` client. If `jira` isn't on `PATH`, run it by its path: `bin/jira` two levels above
this skill's directory. Call it without variables or `cd`: this way Claude Code recognizes read commands by their
prefix and does not ask about them.

## Rule

- **Read freely** when the task needs it.
- **Change and watch only on your human's command or consent** in this session: comment, issue, status, field, link,
  attachment, watching an issue. Before a change, show exactly what will go out (text, fields) and wait for "yes".
  The action is done from their account — colleagues will see it as theirs.
- Write issues briefly and human-readably: the problem and, if any, the proposed solution. Implementation details
  and code names only if your human asks.
- The service adds the "— agent <name>" signature itself; do not put it in the text.
- Never write secrets, passwords or tokens to Jira. Never handle the Jira token: your human runs `jira register`
  (and `token set` for another site) themselves, in their own terminal; the client refuses to take it from you.

## Start

```bash
jira sites --as backend      # Jira sites, your human's token per site, where you may watch to
```

- `--as <name>` is your project name, as in xchg (`xchg agent` → `team:@backend:alice` → `backend`); it can go
  anywhere in the command. Any agent of a registered human reads, changes and watches; what your human may change
  in Jira, you may change — the signature and the journal tell who did it.
- `the jira-relay service is not set` / `not registered` — your human does it once, in their terminal:
  `jira setup https://HOST:8443` (the admin tells the URL and the certificate fingerprint), then `jira register`.

## Jira sites

The service may know several Jira sites by name (`jira sites`). An issue is `DEMO-42` or `main/DEMO-42`. A key without
a site (and `search`, `api`, `create DEMO`, `token`) goes to the site from `--jira NAME` (anywhere in the command),
else to the default site (marked `*`). The same key on
two sites is two different issues. `link` connects issues of one site only.

## How

```bash
jira get DEMO-42 --as backend                # fields, description, comments, links, subtasks; raw JSON: --json
jira get other/DEMO-42 --as backend          # an issue on another site
jira search 'project = DEMO AND status = Review' --limit 20 --as backend
jira transitions DEMO-42 --as backend        # where it can be moved
jira watching --as backend                   # what is watched, where, and by whom
jira log --since 7d --as backend             # what this agent changed; --all for everyone
jira --help                                  # and `jira <command> --help`
```

Changes (only on your human's command):

```bash
jira comment DEMO-42 --as backend <<'TXT'
Fixed the export, ready to check on staging.
TXT
jira create DEMO --type Sub-task --parent DEMO-42 --summary 'Check the export' --as backend <<'TXT'
Issue description.
TXT
jira transition DEMO-42 Review --comment --as backend <<'TXT'
Ready for review.
TXT
jira assign DEMO-42 jdoe --as backend               # - removes the assignee
jira edit DEMO-42 priority=High labels+=agent --as backend
jira link DEMO-42 blocks DEMO-43 --as backend       # blocks / is blocked by / relates to …
jira attach DEMO-42 ./report.pdf --as backend       # up to 10 MB
jira api PUT /rest/api/2/issue/DEMO-42 --as backend <<'JSON'
{"fields": {"duedate": "2026-11-01"}}
JSON
```

Watching (also only on your human's command): issue changes arrive as notes in an xchg hub.

```bash
jira watch DEMO-42 team:@backend --as backend                  # snapshot now, changes afterwards
jira watch DEMO-42 team:@backend:alice --with-subtasks --as backend
jira watch DEMO-42 team:@backend --dry-run --as backend        # show the snapshot, send nothing
jira unwatch DEMO-42 team:@backend:alice --as backend          # leave that address; without one, all of yours
jira subtasks DEMO-42 on --as backend                          # pick up subtasks
```

- Text (comment, description, `api` body) goes on stdin as a quoted heredoc (`<<'TXT'`), so the shell leaves `$` and
  backticks alone. Markup is Jira Server wiki (`*bold*`, `{code}…{code}`), not Markdown.
- `get` prints the issue in the same format as snapshots in the hub.
- After `link` the service prints the link the way Jira shows it — check the direction.
- `watch`: the address is `hub[:@project][:person]` and is always given — ask your human where the updates should go
  if they did not say. The hubs are those the admin connected (`jira sites`). If someone already watches the issue
  there, your human joins the watchers: no second snapshot, one note per change for everyone. `unwatch` only takes
  your human off the watchers; the address stays while someone else watches there. Watching is polled with the
  watchers' own Jira tokens: an issue your human cannot see in Jira cannot be watched by them.

## Not allowed

- Any `DELETE`: deleting an issue, comment or link is done only by people, in Jira.
- `api POST/PUT` outside issues, links, search, versions, components, filters and agile (sprint, backlog, epic):
  administration, permissions, users, schemes.
- Changing anything your human did not ask for.

## When it does not work

- `cannot reach the jira-relay service` / `replied with non-JSON` — the service is down or unreachable (office
  network?). Tell your human; do not fix it yourself.
- `the certificate of … changed` — stop and tell your human: either the admin reissued it (they rerun `jira setup`)
  or something is in between.
- `not registered` / `service token was revoked` — your human runs `jira register` in their terminal (a Jira token
  renewed in Jira is registered the same way).
- `has no Jira token for <site>` — your human runs `jira token set --jira <site>` themselves (it asks for the token
  without echoing it; `token status` / `token rm` take `--jira` too).
- `unknown Jira site` — the error lists the configured sites; use one of them.
- `jira-relay service is X, but this client is Y` — the plugin is outdated: tell your human to update it. You can
  keep working, but you do not know the new commands and rules.
- Timeout — the change may have gone through: check `jira log --since 1h --as …` before retrying.
