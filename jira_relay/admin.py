"""Admin commands on the service host: `jira-relay setup|jira|outbound|user ...`.

Knows both halves (tracker outbounds, gateway users), like cli.py; tracker and gateway do not import it. Users
register themselves (gateway/auth.py); the host only lists and revokes them.
"""
import shutil
import sys
from dataclasses import replace

from . import config
from .config import Config, ConfigError, Outbound, Site
from .gateway.auth import Users
from .jira import AUTH_KINDS, Jira, JiraError, make_auth
from .tracker import ops, outbounds



def say(message: str) -> None:
    print(message)


def cmd_setup(cfg: Config, args) -> int:
    """At container start: connect the outbounds (xchg hubs) of the config."""
    ops.setup(cfg)
    return 0


# --- Jira sites -----------------------------------------------------------------------------------
def read_stdin(what: str) -> str:
    if sys.stdin.isatty():
        raise ConfigError(f'{what} goes on stdin: jira-relay ... < file')
    value = sys.stdin.read().strip()
    if not value:
        raise ConfigError(f'{what}: stdin is empty')
    return value


def cmd_jira(cfg: Config, args) -> int:
    if args.action == 'ls':
        for s in cfg.sites.values():
            secret = 'secret set' if cfg.site_secret_path(s.name).exists() else 'NO SECRET'
            say(f'{s.name:<12} {s.url}  auth {s.auth}, {secret}' + ('  (default)' if s.name == cfg.default_site else ''))
        if not cfg.sites:
            say('no Jira sites: jira-relay jira add <name> <url>')
        return 0
    name = config.check_name('Jira site', args.name)
    if args.action == 'add':
        if name in cfg.sites:
            raise ConfigError(f'Jira site {name} exists')
        config.save(replace(cfg, sites={**cfg.sites, name: Site(name, args.url.rstrip('/'), args.auth)}))
        say(f'Jira site {name} added; service secret: jira-relay jira secret {name} < file')
    elif args.action == 'rm':
        cfg.site(name)
        config.save(replace(cfg, sites={n: s for n, s in cfg.sites.items() if n != name}))
        cfg.site_secret_path(name).unlink(missing_ok=True)
        say(f'Jira site {name} removed (tracked issues of it stay in the state until `jira-relay rm`)')
    elif args.action == 'secret':
        site = cfg.site(name)
        secret = read_stdin('the service secret')
        try:
            login = Jira(site.url, make_auth(site.auth, secret)).whoami()
        except JiraError as e:
            raise ConfigError(f'Jira did not accept the secret ({e})') from None
        config.write_secret(cfg.site_secret_path(name), secret)
        say(f'service secret of {name} saved: Jira acts as {login}')
    return 0


# --- outbounds ------------------------------------------------------------------------------------
def cmd_outbound(cfg: Config, args) -> int:
    if args.action == 'ls':
        for o in cfg.outbounds.values():
            options = ', '.join(f'{k} {v}' for k, v in o.options.items())
            say(f'{o.name:<12} {o.type}  {options}' + (f'  owner {o.owner}' if o.owner else ''))
        if not cfg.outbounds:
            say('no outbounds: jira-relay outbound add <name> --type xchg --remote <git url> --login <bot login>')
        return 0
    name = config.check_name('outbound', args.name)
    if args.action == 'add':
        if name in cfg.outbounds:
            raise ConfigError(f'outbound {name} exists')
        options = {k: v for k, v in (('remote', args.remote), ('login', args.bot_login)) if v}
        owner = config.check_login(args.owner) if args.owner else ''
        cfg = replace(cfg, outbounds={**cfg.outbounds, name: Outbound(name, args.type, options, owner)})
        outbounds.driver(cfg, name)  # validates type and options before saving
        config.save(cfg)
        say(f'outbound {name} added; credentials: jira-relay outbound key {name} < file')
    elif args.action == 'rm':
        if name not in cfg.outbounds:
            raise ConfigError(f'no outbound {name}')
        config.save(replace(cfg, outbounds={n: o for n, o in cfg.outbounds.items() if n != name}))
        shutil.rmtree(cfg.outbound_dir(name), ignore_errors=True)
        say(f'outbound {name} removed (addresses to it stay on tracked issues until `jira-relay to ... rm`)')
    elif args.action == 'key':
        if name not in cfg.outbounds:
            raise ConfigError(f'no outbound {name}')
        config.write_secret(cfg.outbound_dir(name) / 'ssh_key', read_stdin('the write key') + '\n')
        for warning in outbounds.driver(cfg, name).setup():
            say(f'warning: {warning}')
        say(f'outbound {name}: key saved and connected')
    elif args.action == 'hostkey':
        if name not in cfg.outbounds:
            raise ConfigError(f'no outbound {name}')
        config.write_secret(cfg.outbound_dir(name) / 'known_hosts', read_stdin('known_hosts lines (ssh-keyscan HOST)'))
        say(f'outbound {name}: host keys pinned; verify the fingerprint with the hub owner')
    elif args.action == 'owner':
        if name not in cfg.outbounds:
            raise ConfigError(f'no outbound {name}')
        owner = config.check_login(args.owner_login) if args.owner_login else ''
        config.save(replace(cfg, outbounds={**cfg.outbounds, name: replace(cfg.outbounds[name], owner=owner)}))
        say(f'outbound {name}: ' + (f'personal, owner {owner}' if owner else 'shared'))
    return 0


# --- users ----------------------------------------------------------------------------------------
def cmd_user(cfg: Config, args) -> int:
    """Users register themselves with a Jira token (gateway/auth.py); here they are listed and revoked."""
    users = Users(cfg.state_path)
    try:
        if args.action == 'ls':
            for u in users.all():
                tokens = [s for s in cfg.sites if cfg.user_secret_path(u['login'], s).exists()]
                say(f'{u["login"]:<14} registered {u["created"][:10]}, last seen {(u["last_seen"] or "never")[:10]}; '
                    f'Jira: {", ".join(tokens) or "no token"}')
            if not users.all():
                say('no users yet: they register themselves with their Jira token (jira register)')
            return 0
        login = config.check_login(args.login)
        if not users.revoke(login):
            raise ConfigError(f'no user {login}')
        shutil.rmtree(cfg.secrets_dir / 'users' / login, ignore_errors=True)
        say(f'user {login} revoked with their Jira tokens; issues they watched stay tracked (jira-relay ls)')
        return 0
    finally:
        users.db.close()


def add_parsers(sub) -> None:
    p = sub.add_parser('setup', help='container start: connect the outbounds of the config')
    p.set_defaults(handler=cmd_setup)

    p = sub.add_parser('jira', help='Jira sites: ls | add NAME URL [--auth pat] | rm NAME | secret NAME < file')
    p.add_argument('action', choices=('ls', 'add', 'rm', 'secret'))
    p.add_argument('name', nargs='?')
    p.add_argument('url', nargs='?')
    p.add_argument('--auth', default='pat', choices=tuple(AUTH_KINDS))
    p.set_defaults(handler=cmd_jira, need=('name',), need_add=('url',))

    p = sub.add_parser('outbound', help='where changes go: ls | add NAME --type xchg --remote URL --login BOT '
                                        '[--owner LOGIN] | key NAME < file | hostkey NAME < file | owner NAME [LOGIN] | '
                                        'rm NAME')
    p.add_argument('action', choices=('ls', 'add', 'rm', 'key', 'hostkey', 'owner'))
    p.add_argument('name', nargs='?')
    p.add_argument('owner_login', nargs='?', metavar='login', help='owner: whose outbound it is (informational)')
    p.add_argument('--type', default='xchg', choices=tuple(outbounds.TYPES))
    p.add_argument('--remote')
    p.add_argument('--owner')
    p.add_argument('--login', dest='bot_login', help='xchg: the bot login in the hub')
    p.set_defaults(handler=cmd_outbound, need=('name',))

    p = sub.add_parser('user', help='registered users (they register themselves): ls | revoke LOGIN')
    p.add_argument('action', choices=('ls', 'revoke'))
    p.add_argument('login', nargs='?')
    p.set_defaults(handler=cmd_user, need=('login',))


def check_args(args) -> None:
    if getattr(args, 'action', 'ls') == 'ls':
        return
    for name in getattr(args, 'need', ()):
        if not getattr(args, name, None):
            raise ConfigError(f'{args.command} {args.action}: {name.upper()} is required')
    if args.action == 'add':
        for name in getattr(args, 'need_add', ()):
            if not getattr(args, name, None):
                raise ConfigError(f'{args.command} add: {name.upper()} is required')
