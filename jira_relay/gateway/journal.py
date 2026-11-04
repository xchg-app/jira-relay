"""Gateway journal: changes agents made in Jira (reads are not recorded).

The actions table lives in the same state.db as the tracker state (historically), but the tracker never touches it:
when the services split, the journal gets its own file with no code moved.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = '''
create table if not exists actions (
    at      text not null,
    agent   text not null,
    op      text not null,
    target  text not null,
    summary text not null,
    result  text not null
);
'''


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)  # the poll loop may hold the same file
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, agent: str, op: str, target: str, summary: str, result: str) -> None:
        with self.db:
            self.db.execute('insert into actions (at, agent, op, target, summary, result) values (?, ?, ?, ?, ?, ?)',
                            (_now(), agent, op, target, summary, result))

    def since(self, since: str, agent: str | None = None) -> list[sqlite3.Row]:
        query, params = 'select * from actions where at >= ?', [since]
        if agent:
            query, params = query + ' and agent = ?', params + [agent]
        return self.db.execute(query + ' order by rowid', params).fetchall()
