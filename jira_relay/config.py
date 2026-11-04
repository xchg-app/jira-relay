"""Settings: one TOML file in the data directory, secrets in files next to it.

    <data>/config/jira-relay.toml        what is connected: Jira sites, outbounds, users and their agents
    <data>/secrets/jira/<site>           service secret of a Jira site (for pat, the token): polling and the admin
    <data>/secrets/outbound/<name>/      outbound credentials (xchg: ssh_key)
    <data>/secrets/users/<login>/<site>  a user's own Jira secret
    <data>/state/                        tracker state and the gateway journal

The data directory is JIRA_RELAY_DATA (in the container /data). The file is written by `jira-relay jira|outbound|user`
commands; hand edits are fine, but comments are not kept. Secrets never go into the file.

    admin = "alice"
    timezone = "UTC"
    max_attachment = 10485760
    interval = 300

    [jira.main]
    url = "https://jira.example.com"
    auth = "pat"

    [outbound.team]
    type = "xchg"
    remote = "git@git.example.com:team/hub.git"
    login = "jira-bot"

    [outbound.alice]
    type = "xchg"
    remote = "git@xchg.app:alice/personal.git"
    login = "alice.jira-bot"
    owner = "alice"             # personal: only its owner can publish to it

    [users.bob]
    outbounds = ["team"]        # shared outbounds the user may publish to; the admin may use all

    [users.bob.agents.backend]
    projects = ["main/DEMO"]    # where the agent may change Jira: <site>/<PROJECT>, <site>/* or *
    watch_to = "team:@backend"  # where `jira watch` sends updates when no address is given
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .jira import AUTH_KINDS, Jira, make_auth

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')  # site, outbound and agent names
LOGIN_RE = re.compile(r'^[A-Za-z0-9._-]+$')
DEFAULTS = {'timezone': 'Europe/Moscow', 'max_attachment': 10 * 1024 * 1024, 'interval': 300}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Site:
    name: str
    url: str
    auth: str = 'pat'


@dataclass(frozen=True, eq=False)
class Outbound:
    name: str
    type: str
    options: dict = field(default_factory=dict)  # type-specific: xchg — remote, login
    owner: str = ''  # empty — shared (users get it through users.<login>.outbounds); a login — personal


@dataclass(frozen=True)
class Agent:
    projects: tuple[str, ...] = ()  # "<site>/<PROJECT>", "<site>/*" or "*"
    watch_to: str = ''


@dataclass(frozen=True, eq=False)
class User:
    login: str
    outbounds: tuple[str, ...] = ()  # shared outbounds allowed besides the user's own
    agents: dict[str, Agent] = field(default_factory=dict)


@dataclass(frozen=True, eq=False)
class Config:
    data_dir: Path
    admin: str = ''
    tz: str = DEFAULTS['timezone']
    max_attachment: int = DEFAULTS['max_attachment']
    interval: int = DEFAULTS['interval']
    sites: dict[str, Site] = field(default_factory=dict)
    outbounds: dict[str, Outbound] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)

    # --- where things live ------------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.data_dir / 'config' / 'jira-relay.toml'

    @property
    def state_dir(self) -> Path:
        return self.data_dir / 'state'

    @property
    def state_path(self) -> Path:
        return self.state_dir / 'state.db'

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / 'secrets'

    def site_secret_path(self, site: str) -> Path:
        return self.secrets_dir / 'jira' / site

    def user_secret_path(self, login: str, site: str) -> Path:
        return self.secrets_dir / 'users' / login / site

    def outbound_dir(self, name: str) -> Path:
        return self.secrets_dir / 'outbound' / name

    # --- sites ------------------------------------------------------------------------------------
    @property
    def default_site(self) -> str:
        """The first site listed: issue keys without a site go there."""
        if not self.sites:
            raise ConfigError(f'no Jira site in {self.path}: jira-relay jira add <name> <url>')
        return next(iter(self.sites))

    def site(self, name: str | None = None) -> Site:
        name = name or self.default_site
        try:
            return self.sites[name]
        except KeyError:
            raise ConfigError(f'unknown Jira site "{name}" (configured: {", ".join(self.sites) or "none"})') from None

    def jira(self, site: str | None = None) -> Jira:
        """Client with the service secret of the site: polling, snapshots, the admin's agents."""
        s = self.site(site)
        secret = read_secret(self.site_secret_path(s.name))
        if not secret:
            raise ConfigError(f'no secret for Jira site "{s.name}": jira-relay jira secret {s.name} < file')
        return Jira(s.url, make_auth(s.auth, secret))

    def user_jira(self, login: str, site: str | None = None) -> Jira | None:
        """Client acting as the user: the admin uses the service secret, others their own (None — not set yet)."""
        if login == self.admin:
            return self.jira(site)
        s = self.site(site)
        secret = read_secret(self.user_secret_path(login, s.name))
        return Jira(s.url, make_auth(s.auth, secret)) if secret else None

    # --- outbounds --------------------------------------------------------------------------------
    @property
    def hubs(self) -> tuple[str, ...]:
        """Names of all outbounds: the first part of a publishing address."""
        return tuple(self.outbounds)

    def outbounds_of(self, login: str) -> tuple[str, ...] | None:
        """Outbounds the user may publish to; None — all (the admin)."""
        if login == self.admin:
            return None
        user = self.users.get(login)
        allowed = set(user.outbounds) if user else set()
        return tuple(n for n, o in self.outbounds.items() if o.owner == login or (not o.owner and n in allowed))


def read_secret(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def write_secret(path: Path, value: str) -> None:
    """0600 file, written atomically; directories 0700."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value.rstrip('\n') + '\n')
    tmp.replace(path)


def data_dir() -> Path:
    return Path(os.environ.get('JIRA_RELAY_DATA', str(Path.home() / '.local' / 'share' / 'jira-relay'))).expanduser()


# --- reading ---------------------------------------------------------------------------------------
def check_name(kind: str, value: str) -> str:
    if not NAME_RE.match(value):
        raise ConfigError(f'{kind} name "{value}": lowercase letters, digits, ".", "_", "-"')
    return value


def parse(data: dict, directory: Path) -> Config:
    sites = {}
    for name, item in (data.get('jira') or {}).items():
        auth = item.get('auth', 'pat')
        if auth not in AUTH_KINDS:
            raise ConfigError(f'jira.{name}: unknown auth "{auth}" (supported: {", ".join(AUTH_KINDS)})')
        if not item.get('url'):
            raise ConfigError(f'jira.{name}: url is required')
        sites[check_name('Jira site', name)] = Site(name, item['url'].rstrip('/'), auth)
    outbounds = {}
    for name, item in (data.get('outbound') or {}).items():
        item = dict(item)
        kind, owner = item.pop('type', ''), item.pop('owner', '')
        if not kind:
            raise ConfigError(f'outbound.{name}: type is required')
        outbounds[check_name('outbound', name)] = Outbound(name, kind, item, owner)
    users = {}
    for login, item in (data.get('users') or {}).items():
        if not LOGIN_RE.match(login):
            raise ConfigError(f'users.{login}: a login is [A-Za-z0-9._-]')
        agents = {check_name('agent', name): Agent(tuple(a.get('projects') or ()), a.get('watch_to') or '')
                  for name, a in (item.get('agents') or {}).items()}
        users[login] = User(login, tuple(item.get('outbounds') or ()), agents)
    admin = data.get('admin') or ''
    if users and admin not in users:
        raise ConfigError(f'admin "{admin}" must be one of users')
    return Config(data_dir=directory, admin=admin, tz=data.get('timezone', DEFAULTS['timezone']),
                  max_attachment=int(data.get('max_attachment', DEFAULTS['max_attachment'])),
                  interval=int(data.get('interval', DEFAULTS['interval'])),
                  sites=sites, outbounds=outbounds, users=users)


def load(directory: Path | None = None) -> Config:
    directory = directory or data_dir()
    path = directory / 'config' / 'jira-relay.toml'
    try:
        with open(path, 'rb') as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f'cannot read {path}: {e}') from None
    return parse(data, directory)


# --- writing (stdlib has no TOML writer; the schema is small) ---------------------------------------
def _value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_value(v) for v in value) + ']'
    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


def _key(name: str) -> str:
    return name if re.match(r'^[A-Za-z0-9_-]+$', name) else _value(name)


def dump(cfg: Config) -> str:
    lines = ['# jira-relay settings: edit with `jira-relay jira|outbound|user ...`; secrets live in ../secrets', '']
    if cfg.admin:
        lines.append(f'admin = {_value(cfg.admin)}')
    lines += [f'timezone = {_value(cfg.tz)}', f'max_attachment = {cfg.max_attachment}', f'interval = {cfg.interval}']
    for s in cfg.sites.values():
        lines += ['', f'[jira.{_key(s.name)}]', f'url = {_value(s.url)}', f'auth = {_value(s.auth)}']
    for o in cfg.outbounds.values():
        lines += ['', f'[outbound.{_key(o.name)}]', f'type = {_value(o.type)}']
        lines += [f'{_key(k)} = {_value(v)}' for k, v in o.options.items()]
        if o.owner:
            lines.append(f'owner = {_value(o.owner)}')
    for u in cfg.users.values():
        lines += ['', f'[users.{_key(u.login)}]']
        if u.outbounds:
            lines.append(f'outbounds = {_value(u.outbounds)}')
        for name, a in u.agents.items():
            lines += ['', f'[users.{_key(u.login)}.agents.{_key(name)}]', f'projects = {_value(a.projects)}']
            if a.watch_to:
                lines.append(f'watch_to = {_value(a.watch_to)}')
    return '\n'.join(lines) + '\n'


def save(cfg: Config) -> None:
    text = dump(cfg)
    parse(tomllib.loads(text), cfg.data_dir)  # never write a file we could not read back
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.with_name(cfg.path.name + '.tmp')
    tmp.write_text(text)
    tmp.replace(cfg.path)
