# jira-relay

A self-hosted bridge between Jira Server/DC and coding agents, in both directions:

- **Tracker** — follows selected issues on one or more Jira sites and publishes their changes to outbounds (today
  [xchg](https://github.com/xchg-app/xchg) git hubs) as Markdown notes: a snapshot when tracking starts, then one note
  per issue per change. Recipients — people or agents — need no Jira access.
- **Gateway** — lets agents read and change Jira through the service with the `jira` client from the Claude Code plugin:
  each user acts with their own Jira account, every change is made only on the human's command, signed by the agent
  and journaled.

Agents never hold Jira tokens: a user registers once with their Jira token, which stays in the service's data volume,
and agents reach the service over HTTPS with the service token the client keeps.

Architecture, note schema and the gateway's rules: [docs/design.md](docs/design.md).

## Install the service

A docker host your users can reach over the network on one port (the gateway, HTTPS). In a clone of this repository:

```bash
cat > .env <<'EOF'            # host-specific values, outside git (full list at the top of compose.yaml)
APP_UID=1000                  # uid/gid that owns /data in the container
APP_GID=1000
BOT_EMAIL=jira-bot@example.com
JIRA_RELAY_BIND=10.0.0.5      # the host address users reach; docker publishes the port past the firewall
JIRA_RELAY_PORT=8443
EOF
docker compose up -d --build
alias relay='docker compose exec -T tracker jira-relay'

relay jira add main https://jira.example.com           # a Jira site under a name you choose
relay outbound add team --remote git@git.example.com:team/hub.git --login jira-bot   # an xchg hub
relay outbound hostkey team < known_hosts              # the hub's host key (ssh-keyscan; verify the fingerprint)
relay outbound key team < hub_write_key                # its write key; the hub is connected right away
relay tls                                              # the gateway certificate's fingerprint: tell it to the users
```

Two services share one image and one data volume: `tracker` (polling and publishing) and `gateway` (the agents'
port). Everything lives in the volume: `/data/config/jira-relay.toml` (readable and editable, schema in
`jira_relay/config.py`), `/data/secrets` (0600 files) and `/data/state/state.db` (tracked issues, registered users).
The config is reread every cycle and every request; no restart needed. There is no service account in Jira: users
register with their own tokens, and each issue is polled with the token of a user who watches it.

Updating goes to a release tag (rollback is the same step with the old tag):

```bash
git fetch --tags && git checkout v0.6.0 && docker compose up -d --build
docker compose logs --since 2m        # "jira-relay 0.6.0 started: …", "… gateway on https://0.0.0.0:8443 …"
```

Coming from 0.5: `users`, `admin` and agents leave the config file by themselves (the file is rewritten without
them); every user registers (`jira register`); the admin's old addresses have no watcher yet — after registering,
`relay user claim <login>` adopts them, otherwise they are paused at the next cycle.

## Install the client (each user)

The `jira` client and its skill come as a package for the user's coding agent:

| harness | install |
|---|---|
| Claude Code | `/plugin marketplace add xchg-app/jira-relay-claude-code`, then `/plugin install jira-relay@jira-relay` |
| Codex CLI | `codex plugin marketplace add xchg-app/jira-relay-codex`, then `codex plugin add jira-relay@jira-relay` |

Any other agent with a shell: copy `client/bin/jira` somewhere on `PATH` and give it `client/skills/jira/SKILL.md`.

A plugin keeps the client inside itself, so your terminal has no `jira` command: run it by its full path (ask your
agent, or `command -v jira` in its shell; the path changes with the plugin version), or put that `bin/` on `PATH`.

Then once, in a terminal: `jira setup https://HOST:8443` (it pins the service certificate and prints its fingerprint:
compare with what the admin announced, or pass `--fingerprint SHA256` to have it checked), then `jira register` with
your Jira personal access token (asked without echo, verified against Jira; the login is what Jira says). A token for
another site: `jira token set --jira other`. `jira sites --as <project>` shows what is set up. The client warns when
the service is newer than itself.

## Admin commands

```bash
relay jira ls | add NAME URL [--auth pat] | rm NAME
relay outbound ls | add NAME --type xchg --remote URL --login BOT [--owner LOGIN] | key NAME < file
               | hostkey NAME < known_hosts | owner NAME [LOGIN] | rm NAME
relay user ls | revoke LOGIN | claim LOGIN
relay tls
```

- A **Jira site** has a name; issues are `site/KEY` (`main/DEMO-42`), a bare key means the first site.
  Authentication is per site (`auth`); today `pat` (Server/DC personal access token).
- An **outbound** is where changes go; an address is `outbound:target`. For xchg the target is `@project`, `person`,
  `@project:person` or `all`. Every registered user may publish to every outbound; `owner` only says whose it is.
- A **user** registers themselves: the login is their Jira login, the Jira token is kept per site in
  `/data/secrets/users/<login>/<site>`, the service token's hash in `state.db`. `revoke` deletes the user with their
  tokens (they can register again); `claim` makes them the watcher of every address that has none.

## Tracking commands

```bash
relay add DEMO-42 team:@backend me:alice --by alice --with-subtasks   # summary and a snapshot prompt
relay add other/ABC-1 team:@backend --by alice --dry-run   # another site; print the snapshot, record nothing
relay to DEMO-42 add team:@backend:bob --by bob  # one more address; it gets a snapshot right away
relay to DEMO-42 rm me:alice                     # remove an address for everyone (the last one only via rm)
relay show DEMO-42 --by alice
relay ls
relay subtasks DEMO-41 on --yes                  # pick up subtasks of a tracked issue
relay rm DEMO-42                                 # together with picked-up subtasks
relay poll --dry-run                             # what would go out right now
```

`--by LOGIN` says whose Jira token reads the issue and who watches it. `add`, `to`, `subtasks`, `rm` and `poll` take
the same lock as the loop: they wait for the current cycle to finish. Users put issues on tracking themselves with
`jira watch`; in `ls`, the parentheses after an address list its watchers (`login/agent`). An address shared by
several watchers is one address: one snapshot, one note per change.

## What lives where

| What | Where |
|---|---|
| image | `jira-relay:local`: `python:3.12-alpine` + git/bash/openssh/openssl, code in `/opt/jira-relay`, xchg client in `/opt/xchg` (pinned `XCHG_REF`) |
| services | `tracker` (`jira-relay run`), `gateway` (`jira-relay serve`, port 8443 published on `JIRA_RELAY_BIND:JIRA_RELAY_PORT`) |
| settings | `/data/config/jira-relay.toml` in the volume `${JIRA_RELAY_VOLUME:-jira-relay_data}` |
| secrets | `/data/secrets/users/<login>/<site>`, `/data/secrets/outbound/<name>/{ssh_key,known_hosts}`, `/data/secrets/tls/{cert,key}.pem` (0600) |
| state | `/data/state/state.db`: tracked issues, addresses and watchers, deliveries, registered users, the gateway journal |
| xchg | `/data/xchg/` (bot config), `/data/hub-<outbound>/` (hub clones) |
| log | `docker compose logs` (json-file, 3 × 10 MB) |

## Development

```bash
python3 -m unittest discover -s tests -t .
./bin/jira-relay show --raw issue.json     # snapshot from a saved issue JSON, no config or token needed
```

Python 3.12 stdlib only, no dependencies. The agent client and its skill live in `client/` (`bin/jira`,
`skills/jira/SKILL.md`); the manifests of each harness (Claude Code, Codex) in `harness/<name>/`. `tools/package.sh <harness> <dir>` builds a
package locally.

## Releases

```bash
bin/release 0.5.0 --dry-run     # checks and the release text
bin/release 0.5.0               # version everywhere, tag v0.5.0 with the changelog, push
```

The tag starts the `release` workflow: it builds the package for each harness and overwrites its repository
(`xchg-app/jira-relay-claude-code`, `xchg-app/jira-relay-codex`; the workflow needs the `PACKAGE_REPOS_TOKEN` secret with write
access to them), then creates the GitHub Release from the tag. The version lives in `jira_relay/__init__.py`,
`client/bin/jira` and the harness manifests and is changed only by the script. What went in is recorded in [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

## License

MIT
