"""Admin commands on the service host: `jira-relay setup|jira|outbound|user ...`.

Knows both halves (tracker outbounds, gateway users and tokens), like cli.py; tracker and gateway do not import it.
"""
import shutil
import sys
from dataclasses import replace

from . import config
from .config import Agent, Config, ConfigError, Outbound, Site, User
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
            say(f'{o.name:<12} {o.type}  {options}' + (f'  owner {o.owner}' if o.owner else '  shared'))
        if not cfg.outbounds:
            say('no outbounds: jira-relay outbound add <name> --type xchg --remote <git url> --login <bot login>')
        return 0
    name = config.check_name('outbound', args.name)
    if args.action == 'add':
        if name in cfg.outbounds:
            raise ConfigError(f'outbound {name} exists')
        options = {k: v for k, v in (('remote', args.remote), ('login', args.bot_login)) if v}
        cfg = replace(cfg, outbounds={**cfg.outbounds, name: Outbound(name, args.type, options, args.owner or '')})
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
        owner = args.owner_login or ''
        if owner and owner not in cfg.users:
            raise ConfigError(f'no user {owner}')
        config.save(replace(cfg, outbounds={**cfg.outbounds, name: replace(cfg.outbounds[name], owner=owner)}))
        say(f'outbound {name}: ' + (f'personal, owner {owner}' if owner else 'shared'))
    return 0


# --- users and their agents -----------------------------------------------------------------------
def _outbounds(cfg: Config, value) -> tuple[str, ...]:
    names = _csv(value)
    if unknown := [n for n in names if n not in cfg.outbounds]:
        raise ConfigError(f'unknown outbound {", ".join(unknown)} (configured: {", ".join(cfg.outbounds) or "none"})')
    return names


def _projects(cfg: Config, value) -> tuple[str, ...]:
    """"*", "site/*" or "site/PROJ" on a configured site; the project key is uppercased."""
    result = []
    for item in _csv(value):
        if item == '*':
            result.append(item)
            continue
        site, _, project = item.partition('/')
        if not project or not (project == '*' or project.isalnum()):
            raise ConfigError(f'project "{item}": <site>/<PROJECT>, <site>/* or *')
        cfg.site(site)  # unknown site: ConfigError with the configured ones
        result.append(f'{site}/{project.upper()}')
    return tuple(result)


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or '').split(',') if v.strip())


def cmd_user(cfg: Config, args) -> int:
    if args.action == 'ls':
        for u in cfg.users.values():
            role = 'admin' if u.login == cfg.admin else f'outbounds: {", ".join(cfg.outbounds_of(u.login)) or "none"}'
            tokens = [s for s in cfg.sites if u.login == cfg.admin or cfg.user_secret_path(u.login, s).exists()]
            say(f'{u.login:<14} {role}; Jira: {", ".join(tokens) or "no token yet"}')
            for name, a in u.agents.items():
                say(f'  agent {name}: projects {", ".join(a.projects) or "none"}'
                    + (f'; watch_to {a.watch_to}' if a.watch_to else ''))
        return 0
    login = args.login
    if not config.LOGIN_RE.match(login):
        raise ConfigError(f'login "{login}": [A-Za-z0-9._-]')
    if args.action == 'add':
        if login in cfg.users:
            raise ConfigError(f'user {login} exists')
        users = {**cfg.users, login: User(login, _outbounds(cfg, args.outbounds))}
        config.save(replace(cfg, users=users, admin=cfg.admin or login))
        say(f'user {login} added as the admin; their agents act with the service secret of each Jira site' if not cfg.admin
            else f'user {login} added; the user puts their own Jira token: jira token set [--jira SITE] (from their agent client)')
        return 0
    user = cfg.users.get(login)
    if not user:
        raise ConfigError(f'no user {login}')
    if args.action == 'rm':
        if login == cfg.admin:
            raise ConfigError('the admin cannot be removed')
        config.save(replace(cfg, users={n: u for n, u in cfg.users.items() if n != login}))
        shutil.rmtree(cfg.secrets_dir / 'users' / login, ignore_errors=True)
        say(f'user {login} removed with their Jira tokens')
    elif args.action == 'outbounds':
        config.save(replace(cfg, users={**cfg.users, login: replace(user, outbounds=_outbounds(cfg, args.names))}))
        say(f'user {login}: shared outbounds {", ".join(_csv(args.names)) or "none"}')
    elif args.action == 'agent':
        name = config.check_name('agent', args.agent)
        agents = dict(user.agents)
        if args.remove:
            agents.pop(name, None)
        else:
            agents[name] = Agent(_projects(cfg, args.projects), args.watch_to or '')
            if args.watch_to:
                outbounds.parse_address(cfg, args.watch_to)
        config.save(replace(cfg, users={**cfg.users, login: replace(user, agents=agents)}))
        say(f'user {login}: agent {name} ' + ('removed' if args.remove else
            f'projects {", ".join(agents[name].projects) or "none"}'
            + (f'; watch_to {args.watch_to}' if args.watch_to else '')))
    return 0


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
                                        '[--owner LOGIN] | key NAME < file | owner NAME [LOGIN] | rm NAME')
    p.add_argument('action', choices=('ls', 'add', 'rm', 'key', 'hostkey', 'owner'))
    p.add_argument('name', nargs='?')
    p.add_argument('owner_login', nargs='?', metavar='login', help='owner: login of a personal outbound; none — shared')
    p.add_argument('--type', default='xchg', choices=tuple(outbounds.TYPES))
    p.add_argument('--remote')
    p.add_argument('--owner')
    p.add_argument('--login', dest='bot_login', help='xchg: the bot login in the hub')
    p.set_defaults(handler=cmd_outbound, need=('name',))

    p = sub.add_parser('user', help='users: ls | add LOGIN [--outbounds a,b] | rm LOGIN | outbounds LOGIN a,b | '
                                    'agent LOGIN NAME --projects site/PROJ,... [--watch-to ADDR] [--remove]')
    p.add_argument('action', choices=('ls', 'add', 'rm', 'outbounds', 'agent'))
    p.add_argument('login', nargs='?')
    p.add_argument('agent', nargs='?', help='agent: the agent name (its project in xchg); outbounds: comma list')
    p.add_argument('--outbounds')
    p.add_argument('--projects')
    p.add_argument('--watch-to')
    p.add_argument('--remove', action='store_true')
    p.set_defaults(handler=_user, need=('login',))


def _user(cfg: Config, args) -> int:
    if args.action == 'outbounds':
        args.names = args.agent
    elif args.action == 'agent' and not args.agent:
        raise ConfigError('user agent LOGIN NAME --projects ...: the agent name is required')
    return cmd_user(cfg, args)


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
