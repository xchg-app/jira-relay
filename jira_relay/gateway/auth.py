"""Who the user is: self-registration with a Jira token, service tokens for the agent client.

A user registers with their own Jira token for a site: the service asks that Jira who the token belongs to
(`/rest/api/2/myself`) and that name is the login — there is no other way to become somebody. The Jira token is
saved as the user's secret for the site (config.user_secret_path), and the user gets a service token: 32 random
bytes, shown once; only its SHA-256 is stored, in the users table of state.db. Every agent request carries the
service token (`Authorization: Bearer`), and the login comes from it alone, never from the request body.

Registering again with the same login (a renewed Jira token, a lost service token) replaces both tokens.
`jira-relay user revoke LOGIN` on the host deletes the user with their Jira tokens.
"""
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..jira import Jira, JiraError, make_auth
from . import AgentError

SCHEMA = '''
create table if not exists users (
    login      text primary key,
    token_hash text not null unique,
    created    text not null,
    last_seen  text
);
'''


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Users:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)  # the poll loop may hold the same file
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def put(self, login: str, token: str) -> None:
        """New user, or a new service token for an existing one."""
        with self.db:
            self.db.execute('insert into users (login, token_hash, created) values (?, ?, ?) '
                            'on conflict(login) do update set token_hash = excluded.token_hash',
                            (login, token_hash(token), _now()))

    def login_for(self, token: str) -> str | None:
        """The login the service token belongs to; None — unknown or revoked. Notes when the user was last seen."""
        row = self.db.execute('select login from users where token_hash = ?', (token_hash(token),)).fetchone()
        if row:
            with self.db:
                self.db.execute('update users set last_seen = ? where login = ?', (_now(), row['login']))
            return row['login']
        return None

    def exists(self, login: str) -> bool:
        return self.db.execute('select 1 from users where login = ?', (login,)).fetchone() is not None

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute('select * from users order by login').fetchall()

    def revoke(self, login: str) -> bool:
        with self.db:
            return self.db.execute('delete from users where login = ?', (login,)).rowcount > 0


@dataclass(frozen=True)
class Registration:
    login: str
    token: str  # the service token: shown to the user once, never stored


def register(cfg: config.Config, site: str, jira_token: str, make_jira=Jira) -> Registration:
    """Checks the Jira token with the site, saves it as the user's secret, issues a service token."""
    s = cfg.site(site)
    jira_token = jira_token.strip()
    if not jira_token or len(jira_token.split()) != 1:
        raise AgentError('the Jira token is one line (Jira → Profile → Personal Access Tokens)')
    try:
        login = make_jira(s.url, make_auth(s.auth, jira_token)).whoami()
    except JiraError as e:
        raise AgentError(f'{s.name} rejected the token ({e})') from None
    try:
        config.check_login(login)
    except config.ConfigError as e:
        raise AgentError(f'{s.name} knows the token as "{login}": {e}') from None
    config.write_secret(cfg.user_secret_path(login, s.name), jira_token)
    token = secrets.token_urlsafe(32)
    users = Users(cfg.state_path)
    try:
        users.put(login, token)
    finally:
        users.db.close()
    return Registration(login, token)


def authenticate(cfg: config.Config, token: str) -> str:
    """Login for a service token; AgentError when it is not one."""
    users = Users(cfg.state_path)
    try:
        login = users.login_for(token.strip()) if token else None
    finally:
        users.db.close()
    if not login:
        raise AgentError('not registered in jira-relay (or the service token was revoked): register again — '
                         'jira register, with your Jira token')
    return login
