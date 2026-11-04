# jira-relay

A self-hosted bridge between Jira Server/DC and coding agents, in both directions:

- **Tracker** — follows selected issues on one or more Jira sites and publishes their changes to outbounds (today
  [xchg](https://github.com/xchg-app/xchg) git hubs) as Markdown notes: a snapshot when tracking starts, then one note
  per issue per change. Recipients — people or agents — need no Jira access.
- **Gateway** — lets agents read and change Jira through the service with the `jira` client from the Claude Code plugin:
  each user acts with their own Jira account, every change is made only on the human's command, signed by the agent
  and journaled.

Agents never hold Jira tokens: the tokens stay in the service's data volume; agents reach it over ssh as their human.

Architecture, note schema and the gateway's rules: [docs/design.md](docs/design.md).

## Install the service

A docker host your users can ssh to (their agents call the service as `docker exec` over ssh, so users need to be in
the `docker` group there). In a clone of this repository:

```bash
cat > .env <<'EOF'            # host-specific values, outside git (full list at the top of compose.yaml)
APP_UID=1000                  # uid/gid that owns /data in the container
APP_GID=1000
BOT_EMAIL=jira-bot@example.com
EOF
docker compose up -d --build
alias relay='docker compose exec -T jira-relay jira-relay'

relay user add alice                                   # the first user becomes the admin
relay jira add main https://jira.example.com           # a Jira site under a name you choose
relay jira secret main < pat.txt                       # its service PAT: polling, snapshots and the admin's actions
relay outbound add team --remote git@git.example.com:team/hub.git --login jira-bot   # an xchg hub
relay outbound hostkey team < known_hosts              # the hub's host key (ssh-keyscan; verify the fingerprint)
relay outbound key team < hub_write_key                # its write key; the hub is connected right away
relay user agent alice backend --projects main/DEMO --watch-to team:@backend
```

Everything lives in the data volume: `/data/config/jira-relay.toml` (readable and editable, schema in
`jira_relay/config.py`) and `/data/secrets` (0600 files). The service rereads the config every cycle; no restart needed.

Updating goes to a release tag (rollback is the same step with the old tag):

```bash
git fetch --tags && git checkout v0.4.0 && docker compose up -d --build
docker compose logs --since 2m        # "jira-relay 0.4.0 started: …"
```

## Install the plugin (each user)

```
/plugin marketplace add xchg-app/jira-relay
/plugin install jira-relay@jira-relay
```

Then once, in a terminal: `jira setup HOST` (an ssh host or alias where the service runs; `--container NAME` if it is
not `jira-relay-jira-relay-1`), and, for non-admin users, `jira token set --jira main` with their own Jira personal
access token (it is asked without echo and verified against Jira). `jira sites --as <project>` shows what is set up.
The client warns when the service is newer than the plugin.

## Admin commands

```bash
relay jira ls | add NAME URL [--auth pat] | secret NAME < file | rm NAME
relay outbound ls | add NAME --type xchg --remote URL --login BOT [--owner LOGIN] | key NAME < file
               | hostkey NAME < known_hosts | owner NAME [LOGIN] | rm NAME
relay user ls | add LOGIN [--outbounds a,b] | outbounds LOGIN a,b | rm LOGIN
          | agent LOGIN NAME --projects site/PROJ,... [--watch-to ADDR] [--remove]
```

- A **Jira site** has a name; issues are `site/KEY` (`main/DEMO-42`), a bare key means the first site.
  Authentication is per site (`auth`); today `pat` (Server/DC personal access token).
- An **outbound** is where changes go; an address is `outbound:target`. For xchg the target is `@project`, `person`,
  `@project:person` or `all`. A shared outbound is available to the users listed with it (`--outbounds`), a personal
  one (`--owner`) only to its owner; the admin may use all.
- A **user** is an account on the service host (`id -un` over ssh) with the same login in Jira. An agent's `projects`
  are where it may change Jira: `site/PROJ`, `site/*` or `*`. Reading and watching are open to any agent name.

## Tracking commands

```bash
relay add DEMO-42 team:@backend me:alice --with-subtasks   # summary and a snapshot prompt
relay add other/ABC-1 team:@backend --dry-run    # an issue on another site; print the snapshot, record nothing
relay to DEMO-42 add team:@backend:bob           # one more address; it gets a snapshot right away
relay to DEMO-42 rm me:alice                     # remove an address (the last one only via rm)
relay show DEMO-42
relay ls
relay subtasks DEMO-41 on --yes                  # pick up subtasks of a tracked issue
relay rm DEMO-42                                 # together with picked-up subtasks
relay poll --dry-run                             # what would go out right now
relay agent <<<'{"as": "backend", "argv": ["log", "--all"]}'   # the gateway journal
```

`add`, `to`, `subtasks`, `rm` and `poll` take the same lock as the loop: they wait for the current cycle to finish.
Agents put issues on tracking themselves with `jira watch`; in `ls`, the parentheses after an address show which agent
set it.

## What lives where

| What | Where |
|---|---|
| image | `jira-relay:local`: `python:3.12-alpine` + git/bash/openssh, code in `/opt/jira-relay`, xchg client in `/opt/xchg` (pinned `XCHG_REF`) |
| settings | `/data/config/jira-relay.toml` in the volume `${JIRA_RELAY_VOLUME:-jira-relay_data}` |
| secrets | `/data/secrets/jira/<site>`, `/data/secrets/users/<login>/<site>`, `/data/secrets/outbound/<name>/{ssh_key,known_hosts}` (0600) |
| state | `/data/state/state.db`: tracked issues, addresses, deliveries, the gateway journal |
| xchg | `/data/xchg/` (bot config), `/data/hub-<outbound>/` (hub clones) |
| log | `docker compose logs` (json-file, 3 × 10 MB) |

## Development

```bash
python3 -m unittest discover -s tests -t .
./bin/jira-relay show --raw issue.json     # snapshot from a saved issue JSON, no config or token needed
```

Python 3.12 stdlib only, no dependencies. The plugin lives in `plugin/` (`bin/jira`, `skills/jira/SKILL.md`), the
marketplace manifest in `.claude-plugin/`.

## Releases

```bash
bin/release 0.4.0 --dry-run     # checks and the release text
bin/release 0.4.0               # version (service, client, plugin.json), tag v0.4.0, push, GitHub Release (gh)
```

The version lives in `jira_relay/__init__.py`, `plugin/bin/jira` and `plugin/.claude-plugin/plugin.json` and is
changed only by the script. What went in is recorded in [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

## License

MIT
