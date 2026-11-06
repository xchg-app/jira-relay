"""State on the host (sqlite): what we track, where we publish, and what was already delivered to each address.

- Issues are "<site>/<KEY>" (a Jira site from the config and the issue key).
- tracked: tracked issues; subtasks picked up with their parent have parent_key.
- addresses: publishing addresses of top-level issues; subtasks are published to their parent's addresses.
- owners: who watches the issue at an address (login; agent is the last agent of theirs that asked). Two users
  watching one issue into one address share the address: one record, one note per change. An address lives while it
  has owners (an owner is removed by `jira unwatch`); an address without owners was added on the host and is
  removed there. Polling runs with an owner's Jira token (poll.py).
- deliveries: the last delivered fingerprint per (issue, address) pair. Each address has its own pair:
  if one hub is unreachable, the note still goes to the others and is not duplicated on retry.

The same file holds the actions table, the agent gateway journal (gateway/journal.py); the tracker does not touch it.
"""
import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = '''
create table if not exists tracked (
    key           text primary key,
    with_subtasks integer not null default 0,
    parent_key    text,
    added_at      text not null
);
create table if not exists addresses (
    key      text not null,
    address  text not null,
    primary key (key, address)
);
create table if not exists owners (
    key      text not null,
    address  text not null,
    login    text not null,
    agent    text not null default '',
    added_at text not null,
    primary key (key, address, login)
);
create table if not exists deliveries (
    key             text not null,
    address         text not null,
    fingerprint     text not null,
    last_history_id integer not null default 0,
    posted_at       text not null,
    primary key (key, address)
);
create table if not exists meta (
    name  text primary key,
    value text
);
'''


@contextmanager
def lock(state_dir: Path):
    """Single writer: the poll cycle and manual commands must not touch sqlite and the hub clones at the same time."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / 'lock', 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)  # the gateway writes its journal to the same file while the cycle holds the database
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Up to 0.5: addresses.added_by held one agent name per address ('<login>/<agent>', or a bare agent name for
        the admin's). It becomes an owner; a bare name gives no owner: `jira-relay user claim LOGIN` adopts those."""
        columns = [r['name'] for r in self.db.execute('pragma table_info(addresses)')]
        if 'added_by' not in columns:
            return
        with self.db:
            for r in self.db.execute('select key, address, added_by from addresses where added_by != \'\'').fetchall():
                login, _, agent = r['added_by'].rpartition('/')
                if login:
                    self.db.execute('insert or ignore into owners (key, address, login, agent, added_at) values (?, ?, ?, ?, ?)',
                                    (r['key'], r['address'], login, agent, _now()))
            self.db.execute('alter table addresses drop column added_by')

    # --- what we track ---------------------------------------------------------------------------
    def tracked(self, key: str) -> sqlite3.Row | None:
        return self.db.execute('select * from tracked where key = ?', (key,)).fetchone()

    def all_tracked(self) -> list[sqlite3.Row]:
        return self.db.execute('select * from tracked order by coalesce(parent_key, key), parent_key is not null, key').fetchall()

    def subtasks(self, key: str) -> list[str]:
        return [r['key'] for r in self.db.execute('select key from tracked where parent_key = ? order by key', (key,))]

    def track(self, key: str, with_subtasks: bool, parent_key: str | None) -> None:
        with self.db:
            self.db.execute('insert into tracked (key, with_subtasks, parent_key, added_at) values (?, ?, ?, ?)',
                            (key, int(with_subtasks), parent_key, _now()))

    def set_with_subtasks(self, key: str, value: bool) -> None:
        with self.db:
            self.db.execute('update tracked set with_subtasks = ? where key = ?', (int(value), key))

    def untrack(self, key: str, with_subtasks: bool = True) -> list[str]:
        """Remove the issue and (by default) the subtasks picked up with it. Returns the removed keys."""
        keys = [key] + (self.subtasks(key) if with_subtasks else [])
        keys = [k for k in keys if self.tracked(k)]
        with self.db:
            for k in keys:
                for table in ('tracked', 'addresses', 'owners', 'deliveries'):
                    self.db.execute(f'delete from {table} where key = ?', (k,))
        return keys

    # --- where we publish ------------------------------------------------------------------------
    def addresses(self, key: str) -> list[str]:
        return [r['address'] for r in self.db.execute('select address from addresses where key = ? order by rowid', (key,))]

    def delivery_addresses(self, key: str) -> list[str]:
        """Addresses of the issue; for a subtask, its parent's addresses."""
        row = self.tracked(key)
        return self.addresses(row['parent_key'] or key) if row else []

    def owners(self, key: str) -> dict[str, list[str]]:
        """address → its owners as '<login>/<agent>' (or just the login), in the order they came."""
        result = {a: [] for a in self.addresses(key)}
        for r in self.db.execute('select address, login, agent from owners where key = ? order by rowid', (key,)):
            result.setdefault(r['address'], []).append(f'{r["login"]}/{r["agent"]}' if r['agent'] else r['login'])
        return result

    def owner_logins(self, key: str) -> list[str]:
        """Who watches the issue (a subtask: its parent), first owner first: polling runs with their tokens."""
        row = self.tracked(key)
        top = (row['parent_key'] or key) if row else key
        return list(dict.fromkeys(r['login'] for r in self.db.execute(
            'select login from owners where key = ? order by rowid', (top,))))

    def is_owner(self, key: str, address: str, login: str) -> bool:
        return self.db.execute('select 1 from owners where key = ? and address = ? and login = ?',
                               (key, address, login)).fetchone() is not None

    def add_address(self, key: str, address: str, login: str = '', agent: str = '') -> None:
        """The address, and the owner when given (an existing owner of it only updates their agent)."""
        with self.db:
            self.db.execute('insert or ignore into addresses (key, address) values (?, ?)', (key, address))
            if login:
                self.db.execute('insert into owners (key, address, login, agent, added_at) values (?, ?, ?, ?, ?) '
                                'on conflict(key, address, login) do update set agent = excluded.agent',
                                (key, address, login, agent, _now()))

    def remove_owner(self, key: str, address: str, login: str) -> int:
        """Returns how many owners the address still has."""
        with self.db:
            self.db.execute('delete from owners where key = ? and address = ? and login = ?', (key, address, login))
        return self.db.execute('select count(*) from owners where key = ? and address = ?', (key, address)).fetchone()[0]

    def ownerless(self) -> list[tuple[str, str]]:
        return [(r['key'], r['address']) for r in self.db.execute(
            'select a.key, a.address from addresses a left join owners o on o.key = a.key and o.address = a.address '
            'where o.login is null order by a.rowid')]

    def remove_address(self, key: str, address: str) -> None:
        with self.db:
            self.db.execute('delete from addresses where key = ? and address = ?', (key, address))
            self.db.execute('delete from owners where key = ? and address = ?', (key, address))
            for k in [key] + self.subtasks(key):
                self.db.execute('delete from deliveries where key = ? and address = ?', (k, address))

    # --- what was delivered ----------------------------------------------------------------------
    def delivery(self, key: str, address: str) -> tuple[dict, int] | None:
        row = self.db.execute('select fingerprint, last_history_id from deliveries where key = ? and address = ?',
                              (key, address)).fetchone()
        return (json.loads(row['fingerprint']), row['last_history_id']) if row else None

    def save_delivery(self, key: str, address: str, issue: dict, last_history_id: int) -> None:
        with self.db:
            self.db.execute(
                'insert into deliveries (key, address, fingerprint, last_history_id, posted_at) values (?, ?, ?, ?, ?) '
                'on conflict(key, address) do update set fingerprint = excluded.fingerprint, '
                'last_history_id = excluded.last_history_id, posted_at = excluded.posted_at',
                (key, address, json.dumps(issue, ensure_ascii=False, sort_keys=True), last_history_id, _now()),
            )

    # --- service ---------------------------------------------------------------------------------
    def get_meta(self, name: str) -> str | None:
        row = self.db.execute('select value from meta where name = ?', (name,)).fetchone()
        return row['value'] if row else None

    def del_meta(self, name: str) -> None:
        with self.db:
            self.db.execute('delete from meta where name = ?', (name,))

    def set_meta(self, name: str, value: str) -> None:
        with self.db:
            self.db.execute('insert into meta (name, value) values (?, ?) on conflict(name) do update set value = excluded.value', (name, value))
