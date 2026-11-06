"""Poll cycle: issues updated since the last poll -> one note per issue to each of its addresses.

The query window is relative (`updated >= "-Nm"`): JQL evaluates absolute time in the account's time zone
and to the minute, a relative window does not depend on that. The OVERLAP guards against clock skew;
duplicates are cut off by comparing fingerprints.

Delivery is tracked per (issue, address) pair: a note to an unreachable hub is retried on the next cycle
and is not duplicated to the other addresses. The last poll time moves only if the whole cycle passed
without errors: otherwise an issue with an unsent note would fall out of the next window.

There is no service account: a site is polled with the watchers' own tokens (poll_site). Issues are grouped by
their first watcher with a token for the site, one Poller per watcher. A token Jira rejects (401/403) sends its
issues to the next watcher; an issue a watcher cannot see (403/404) likewise. An issue no watcher can read is
"paused": one note to its addresses, then silence until a watcher registers again with a working token.
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
REJECTED = (401, 403)  # a search with a bad token


def log(message: str) -> None:
    print(f'{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}', flush=True)


def jql(field: str, keys: list[str], since_minutes: int | None) -> str:
    query = f'{field} in ({", ".join(keys)})'
    return f'{query} AND updated >= "-{since_minutes}m"' if since_minutes else query


def polling_owner(cfg, state, ref: str, skip: set[str] = frozenset()) -> tuple[str, object] | None:
    """The first watcher of the issue (login, Jira) with a token for its site, not in skip; None — nobody."""
    site = ref.partition('/')[0]
    for login in state.owner_logins(ref):
        if login not in skip and (jira := cfg.user_jira(login, site)):
            return login, jira
    return None


def polling_jira(cfg, state, ref: str):
    found = polling_owner(cfg, state, ref)
    return found[1] if found else None


def window(state, site: str, started: datetime) -> int | None:
    last = state.get_meta(f'last_poll:{site}')
    return ceil((started - datetime.fromisoformat(last) + OVERLAP).total_seconds() / 60) if last else None


def poll_site(cfg, state, post, site: str, dry_run: bool = False, now: datetime | None = None) -> int:
    """One cycle over a site with the watchers' tokens (module docstring). Returns 1 if anything failed."""
    started = now or datetime.now(timezone.utc)
    since = window(state, site, started)
    prefix = f'{site}/'
    pending = {r['key'] for r in state.all_tracked() if r['key'].startswith(prefix) and not r['parent_key']}
    tried: dict[str, set[str]] = {ref: set() for ref in pending}
    dead: set[str] = set()
    failed = False
    while pending:
        groups: dict[str, list[str]] = {}
        paused = []
        for ref in sorted(pending):
            if found := polling_owner(cfg, state, ref, tried[ref] | dead):
                groups.setdefault(found[0], []).append(ref)
            else:
                paused.append(ref)
        pending = set()
        for login, refs in groups.items():
            poller = Poller(cfg, state, cfg.user_jira(login, site), post, dry_run, site,
                            keys=[ref[len(prefix):] for ref in refs])
            try:
                failed |= bool(poller.run(since=since))
            except JiraError as e:
                if e.status not in REJECTED:
                    raise
                log(f'{site}: Jira rejects the token of {login} ({e.status}); their issues go to other watchers')
                dead.add(login)
                for ref in refs:
                    tried[ref].add(login)
                pending |= set(refs)
                continue
            for key, status in poller.forbidden.items():
                tried[prefix + key].add(login)
                if polling_owner(cfg, state, prefix + key, tried[prefix + key] | dead):
                    pending.add(prefix + key)
                else:
                    poller.unavailable(key, status)
            for ref in refs:
                if ref not in pending and ref not in poller.forbidden and state.get_meta(f'paused:{ref}'):
                    state.del_meta(f'paused:{ref}')
                    log(f'{ref}: polling resumed with the token of {login}')
        for ref in paused:
            if state.get_meta(f'paused:{ref}'):
                continue
            key = ref[len(prefix):]
            body = render.paused(key, cfg.site(site).url, state.owner_logins(ref))
            sent = True
            for address in state.delivery_addresses(ref):
                try:
                    post(address, f'{key.lower()}-paused', render.browse_url(cfg.site(site).url, key), body, dry_run)
                except HubError as e:
                    sent = False
                    log(f'{key} → {address}: error: {e}')
            if sent and not dry_run:
                state.set_meta(f'paused:{ref}', started.isoformat())
            log(f'{key}: paused, no watcher with a working Jira token')
    if not failed and not dry_run:
        state.set_meta(f'last_poll:{site}', started.isoformat())
    return 1 if failed else 0


class Poller:
    """One Jira site, one token, the given top-level keys (None: every top-level issue of the site) with their
    subtasks. In the state issues are "<site>/<KEY>"; Jira and notes see the bare key."""

    def __init__(self, cfg, state, jira, post, dry_run: bool = False, site: str | None = None,
                 keys: list[str] | None = None):
        self.cfg, self.state, self.jira, self.post, self.dry_run = cfg, state, jira, post, dry_run
        self.site = site or cfg.default_site
        self.url = cfg.site(self.site).url
        self.keys = keys
        self.failed = False
        self.seen: set[str] = set()
        self.forbidden: dict[str, int] = {}  # keys this token cannot see → status; the caller decides

    def ref(self, key: str) -> str:
        return f'{self.site}/{key}'

    def _mine(self, rows) -> list[str]:
        prefix = f'{self.site}/'
        keys = [r['key'][len(prefix):] for r in rows if r['key'].startswith(prefix)
                and (self.keys is None or (r['parent_key'] or r['key'])[len(prefix):] in self.keys)]
        return [k for k in keys if k not in self.forbidden]

    def run(self, now: datetime | None = None, since: int | None = None) -> int:
        """since: the query window in minutes (default: from the site's last poll); the caller moves the window."""
        if since is None:
            since = window(self.state, self.site, now or datetime.now(timezone.utc))
        self._query('key', self._mine(self.state.all_tracked()), since)
        # parents are queried after the pass over keys: unavailable ones are known by then
        self._query('parent', self._mine(r for r in self.state.all_tracked() if r['with_subtasks']), since)
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
        """JQL fails as a whole if any key is missing. Find such keys one by one; a top-level key this token cannot
        see goes to self.forbidden (with its subtasks: they are not queried either)."""
        alive = []
        for key in keys:
            try:
                self.jira.issue(key)
                alive.append(key)
            except JiraError as e:
                if e.status not in UNAVAILABLE:
                    raise
                if removable:
                    row = self.state.tracked(self.ref(key))
                    top = row['parent_key'][len(self.site) + 1:] if row and row['parent_key'] else key
                    self.forbidden[top] = e.status
        return [k for k in alive if k not in self.forbidden]

    def unavailable(self, key: str, status: int) -> None:
        """One "unavailable" note to the addresses, then the issue (with subtasks) is no longer tracked."""
        body = render.unavailable(key, status, self.url)
        sent = [self._guarded(f'{key} → {address}', self.post, address, f'{key.lower()}-unavailable',
                              render.browse_url(self.url, key), body, self.dry_run)
                for address in self.state.delivery_addresses(self.ref(key))]
        if all(sent) and not self.dry_run:
            self.state.untrack(self.ref(key))
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
