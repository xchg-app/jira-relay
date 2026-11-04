"""Poll cycle: issues updated since the last poll -> one note per issue to each of its addresses.

The query window is relative (`updated >= "-Nm"`): JQL evaluates absolute time in the account's time zone
and to the minute, a relative window does not depend on that. The OVERLAP guards against clock skew;
duplicates are cut off by comparing fingerprints.

Delivery is tracked per (issue, address) pair: a note to an unreachable hub is retried on the next cycle
and is not duplicated to the other addresses. The last poll time moves only if the whole cycle passed
without errors: otherwise an issue with an unsent note would fall out of the next window.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from .. import render
from .attachments import prepare
from .diff import activity, diff
from .hub import HubError
from ..issue import last_history_id, normalize
from ..jira import JiraError
from ..mask import mask

OVERLAP = timedelta(minutes=5)
BATCH = 100
UNAVAILABLE = (403, 404)


def log(message: str) -> None:
    print(f'{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}', flush=True)


def jql(field: str, keys: list[str], since_minutes: int | None) -> str:
    query = f'{field} in ({", ".join(keys)})'
    return f'{query} AND updated >= "-{since_minutes}m"' if since_minutes else query


class Poller:
    """One Jira site per poller. In the state issues are "<site>/<KEY>"; Jira and notes see the bare key."""

    def __init__(self, cfg, state, jira, post, dry_run: bool = False, site: str | None = None):
        self.cfg, self.state, self.jira, self.post, self.dry_run = cfg, state, jira, post, dry_run
        self.site = site or cfg.default_site
        self.url = cfg.site(self.site).url
        self.failed = False
        self.seen: set[str] = set()

    def ref(self, key: str) -> str:
        return f'{self.site}/{key}'

    def _keys(self, rows) -> list[str]:
        prefix = f'{self.site}/'
        return [r['key'][len(prefix):] for r in rows if r['key'].startswith(prefix)]

    def run(self, now: datetime | None = None) -> int:
        started = now or datetime.now(timezone.utc)
        last = self.state.get_meta(f'last_poll:{self.site}')
        since = ceil((started - datetime.fromisoformat(last) + OVERLAP).total_seconds() / 60) if last else None

        self._query('key', self._keys(self.state.all_tracked()), since)
        # parents are queried after the pass over keys: unavailable ones are untracked by then
        self._query('parent', self._keys(r for r in self.state.all_tracked() if r['with_subtasks']), since)

        if not self.failed and not self.dry_run:
            self.state.set_meta(f'last_poll:{self.site}', started.isoformat())
        return 1 if self.failed else 0

    def _query(self, field: str, keys: list[str], since: int | None) -> None:
        for i in range(0, len(keys), BATCH):
            chunk = keys[i:i + BATCH]
            try:
                raws = list(self.jira.search(jql(field, chunk, since)))
            except JiraError as e:
                if e.status != 400:
                    raise  # network or auth: the whole cycle fails, the window does not move
                chunk = self._drop_unavailable(chunk, removable=field == 'key')
                raws = list(self.jira.search(jql(field, chunk, since))) if chunk else []
            for raw in raws:
                if raw['key'] not in self.seen:
                    self.seen.add(raw['key'])
                    self._handle(raw)

    def _guarded(self, label: str, action, *args) -> bool:
        try:
            action(*args)
            return True
        except (HubError, JiraError) as e:
            self.failed = True
            log(f'{label}: error: {e}')
            return False

    def _drop_unavailable(self, keys: list[str], removable: bool) -> list[str]:
        """JQL fails as a whole if any key is missing. Find such keys one by one."""
        alive = []
        for key in keys:
            try:
                self.jira.issue(key)
                alive.append(key)
            except JiraError as e:
                if e.status not in UNAVAILABLE:
                    raise
                if removable:
                    self._unavailable(key, e.status)
        return alive

    def _unavailable(self, key: str, status: int) -> None:
        body = render.unavailable(key, status, self.url)
        sent = [self._guarded(f'{key} → {address}', self.post, address, f'{key.lower()}-unavailable',
                              render.browse_url(self.url, key), body, self.dry_run)
                for address in self.state.delivery_addresses(self.ref(key))]
        if all(sent) and not self.dry_run:
            self.state.untrack(self.ref(key), with_subtasks=False)
        log(f'{key}: unavailable ({status})' + (', no longer tracked' if all(sent) else ''))

    def _handle(self, raw: dict) -> None:
        key, issue = raw['key'], normalize(raw)
        if self.state.tracked(self.ref(key)) is None and not self._adopt_subtask(issue):
            return
        for address in self.state.delivery_addresses(self.ref(key)):
            self._guarded(f'{key} → {address}', self._deliver, key, address, raw, issue)

    def _adopt_subtask(self, issue: dict) -> bool:
        """A new subtask of an issue tracked "with subtasks" gets tracked; snapshots go out in _deliver."""
        key, parent = issue['key'], (issue['parent'] or {}).get('key')
        parent_row = self.state.tracked(self.ref(parent)) if parent else None
        if not (parent_row and parent_row['with_subtasks']):
            log(f'{key}: came from Jira but is not tracked, skipping')
            return False
        if self.dry_run:
            for address in self.state.addresses(self.ref(parent)):
                self._post_snapshot(key, address, issue)
            return False
        self.state.track(self.ref(key), False, self.ref(parent))
        log(f'{key}: new subtask of {parent}')
        return True

    def _deliver(self, key: str, address: str, raw: dict, issue: dict) -> None:
        previous = self.state.delivery(self.ref(key), address)
        if previous is None:
            # address without a delivered state: a new subtask or an address added without a snapshot
            self._post_snapshot(key, address, issue)
        elif changes := diff(previous[0], issue):
            added = (changes.get('attachments') or {}).get('added', [])
            with tempfile.TemporaryDirectory() as tmp:
                files, notes = prepare(self.jira, added, self.cfg.max_attachment, Path(tmp))
                body, masked = mask(render.change(issue, changes, self.url, self.cfg.tz,
                                                  activity(raw, previous[1], changes), notes))
                self.post(address, render.change_slug(key, changes), render.browse_url(self.url, key),
                          body, self.dry_run, attach=files)
            log(f'{key} → {address}: {render.change_summary(changes)}'
                + (f', files: {len(files)}' if files else '') + (f' (secrets hidden: {masked})' if masked else ''))
        if not self.dry_run:
            # even without significant changes: a fresh last_history_id keeps noisy history out of the period
            self.state.save_delivery(self.ref(key), address, issue, last_history_id(raw))

    def _post_snapshot(self, key: str, address: str, issue: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files, notes = prepare(self.jira, issue['attachments'], self.cfg.max_attachment, Path(tmp))
            body, masked = mask(render.snapshot(issue, self.url, self.cfg.tz, with_subtasks=False, notes=notes))
            self.post(address, f'{key.lower()}-snapshot', render.browse_url(self.url, key),
                      body, self.dry_run, attach=files)
        log(f'{key} → {address}: snapshot' + (f', files: {len(files)}' if files else '')
            + (f' (secrets hidden: {masked})' if masked else ''))
