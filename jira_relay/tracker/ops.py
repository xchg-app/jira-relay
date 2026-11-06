"""Tracker operations: what to track, to which addresses, snapshots and the poll cycle.

Called by the command line on the service host (cli.py) and by the agent gateway through tracker/api.py. The result is printed
to stdout, a refusal is TrackerError. The caller takes the cycle lock: cli for LOCKED, api by itself.

Issues are "<site>/<KEY>" (parse_ref): a bare key belongs to the default site. Jira sees the bare key.

snapshot: what to do with snapshots for new addresses and issues: 'ask' (ask in the terminal), 'send', 'none'
(only changes from now on), 'dry' (print, do not send or remember anything).

owner: who asks, (login, agent); None — a host command, the address gets no owner. Two owners of one address share
it (state.py): the second gets no snapshot, the issue is published there once.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

from .. import config, render
from ..issue import last_history_id, normalize
from ..jira import JiraError
from ..mask import mask
from . import hub, outbounds
from .attachments import prepare
from .poll import Poller, log
from .state import State

KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*-\d+$')
SNAPSHOT_MODES = ('ask', 'send', 'none', 'dry')


class TrackerError(Exception):
    pass


def parse_ref(cfg: config.Config, value: str) -> str:
    """"main/DEMO-1" or "DEMO-1" (default site) → "<site>/<KEY>"."""
    site, _, key = value.strip().rpartition('/')
    key = key.upper()
    if not KEY_RE.match(key):
        raise TrackerError(f'"{value}" does not look like a Jira issue (e.g. DEMO-42 or {cfg.default_site}/DEMO-42)')
    try:
        return f'{cfg.site(site or None).name}/{key}'
    except config.ConfigError as e:
        raise TrackerError(str(e)) from None


def split(ref: str) -> tuple[str, str]:
    site, _, key = ref.partition('/')
    return site, key


def parse_addresses(cfg: config.Config, values: list[str]) -> list[str]:
    try:
        parsed = [outbounds.parse_address(cfg, value) for value in values]
    except outbounds.OutboundError as e:
        raise TrackerError(str(e)) from None
    return list(dict.fromkeys(parsed))


def open_state(cfg: config.Config) -> State:
    return State(cfg.state_path)


def render_snapshot(cfg: config.Config, raw: dict, with_subtasks: bool,
                    notes: dict[str, str] | None = None, site: str | None = None) -> tuple[dict, str, int]:
    issue = normalize(raw)
    url = cfg.site(site).url if cfg.sites else 'https://jira.example'  # show --raw without a config
    body, masked = mask(render.snapshot(issue, url, cfg.tz, with_subtasks, notes))
    return issue, body, masked


def show(cfg: config.Config, key: str | None, with_subtasks: bool, raw_file: str | None = None) -> None:
    """Issue snapshot to stdout, sending nothing; raw_file: issue JSON from a file instead of Jira."""
    if raw_file:
        raws = [json.loads(Path(raw_file).read_text())]
        site = cfg.default_site if cfg.sites else None
    else:
        site, key = split(parse_ref(cfg, key))
        jira = cfg.jira(site)
        raws = [jira.issue(key)] + (list(jira.search(f'parent = {key}')) if with_subtasks else [])
    for i, raw in enumerate(raws):
        _, body, masked = render_snapshot(cfg, raw, with_subtasks=with_subtasks and i == 0, site=site)
        if i:
            print('\n' + '=' * 80 + '\n')
        print(body, end='')
        if masked:
            print(f'{raw["key"]}: secrets hidden: {masked}', file=sys.stderr)


def publish_snapshots(cfg: config.Config, state: State, jira, site: str, found: list[tuple[dict, str | None]],
                      addresses: list[str], with_subtasks: bool, snapshot: str, directory: Path) -> list[str]:
    """Snapshots of the found issues to the addresses: summary, send prompt, sending, delivery record.

    found: pairs (raw JSON from the site, parent ref or None). An issue's attachments are downloaded once and go to
    all addresses. An address that already has the snapshot is skipped, so a rerun after a failure does
    not duplicate. Returns the issue refs.
    """
    if snapshot not in SNAPSHOT_MODES:
        raise ValueError(f'snapshot: {snapshot}')
    dry_run = snapshot == 'dry'
    notes = []
    for raw, parent in found:
        key = raw['key']
        files, marks = prepare(jira, normalize(raw)['attachments'], cfg.max_attachment, directory / key)
        issue, body, masked = render_snapshot(cfg, raw, with_subtasks=with_subtasks and parent is None, notes=marks,
                                              site=site)
        notes.append((raw, issue, parent, body, files))
        print(f'{render.brief(issue)}{f" (subtask of {parent})" if parent else ""}: '
              f'description {len(issue["description"])} chars, comments {len(issue["comments"])}, '
              f'attachments {len(issue["attachments"])} (to the hub {len(files)}); snapshot {len(body)} chars'
              + (f'; secrets hidden: {masked}' if masked else ''))
    if not notes:
        return []

    send = snapshot in ('send', 'dry')
    if snapshot == 'ask':
        if not sys.stdin.isatty():
            raise TrackerError('no one to ask about the snapshot: add --yes or --no-snapshot')
        answer = input(f'Send snapshots ({len(notes)}) to {", ".join(addresses)}? [y/N] ')
        send = answer.strip().lower() in ('y', 'yes', 'д', 'да')

    post = outbounds.poster(cfg)
    for raw, issue, parent, body, files in notes:
        key = issue['key']
        ref = f'{site}/{key}'
        for target in addresses:
            if state.delivery(ref, target):
                continue
            if send:
                out = post(target, f'{key.lower()}-snapshot', render.browse_url(cfg.site(site).url, key),
                           body, dry_run, attach=files)
                if out:
                    print(out)
            if not dry_run:
                state.save_delivery(ref, target, issue, last_history_id(raw))
        if not dry_run and not state.tracked(ref):
            state.track(ref, with_subtasks and parent is None, parent)
    if dry_run:
        print('dry-run: nothing written to the outbounds or the state', file=sys.stderr)
    return [f'{site}/{issue["key"]}' for _, issue, _, _, _ in notes]


def _subtasks_in_jira(jira, state: State, site: str, ref: str) -> list[tuple[dict, str]]:
    """Subtasks of the issue in Jira that are not tracked yet, as (raw, parent ref)."""
    return [(raw, ref) for raw in jira.search(f'parent = {split(ref)[1]}') if not state.tracked(f'{site}/{raw["key"]}')]


Owner = tuple[str, str] | None


def _owner(owner: Owner) -> tuple[str, str]:
    return owner or ('', '')


def add(cfg: config.Config, ref: str, addresses: list[str], with_subtasks: bool, snapshot: str,
        owner: Owner = None) -> None:
    ref, addresses = parse_ref(cfg, ref), parse_addresses(cfg, addresses)
    state = open_state(cfg)
    if row := state.tracked(ref):
        where = f'subtask of {row["parent_key"]}' if row['parent_key'] else ', '.join(state.addresses(ref))
        raise TrackerError(f'{ref} is already tracked ({where}); to add an address: jira-relay to {ref} add <address>')

    site, key = split(ref)
    jira = cfg.jira(site)
    found = [(jira.issue(key), None)] + (_subtasks_in_jira(jira, state, site, ref) if with_subtasks else [])
    with tempfile.TemporaryDirectory() as tmp:
        refs = publish_snapshots(cfg, state, jira, site, found, addresses, with_subtasks, snapshot, Path(tmp))
    if snapshot != 'dry':
        for target in addresses:
            state.add_address(ref, target, *_owner(owner))
        print(f'tracking → {", ".join(addresses)}: {", ".join(refs)}')


def _top_level(state: State, ref: str):
    row = state.tracked(ref)
    if not row:
        raise TrackerError(f'{ref} is not tracked: jira-relay add {ref} <address>')
    if row['parent_key']:
        raise TrackerError(f'{ref} is a subtask of {row["parent_key"]}: addresses are set on the parent')
    return row


def add_address(cfg: config.Config, ref: str, target: str, snapshot: str, owner: Owner = None) -> None:
    """One more address of a tracked issue: it gets snapshots of the issue and its subtasks right away."""
    ref, (target,) = parse_ref(cfg, ref), parse_addresses(cfg, [target])
    state = open_state(cfg)
    row = _top_level(state, ref)
    current = state.addresses(ref)
    if target in current:
        raise TrackerError(f'{ref} is already published to {target}')
    site, key = split(ref)
    jira = cfg.jira(site)
    found = [(jira.issue(key), None)] + [(jira.issue(split(sub)[1]), ref) for sub in state.subtasks(ref)]
    with tempfile.TemporaryDirectory() as tmp:
        publish_snapshots(cfg, state, jira, site, found, [target], bool(row['with_subtasks']), snapshot, Path(tmp))
    if snapshot != 'dry':
        state.add_address(ref, target, *_owner(owner))
        print(f'{ref}: published to {", ".join(current + [target])}')


def remove_address(cfg: config.Config, ref: str, target: str) -> None:
    ref, (target,) = parse_ref(cfg, ref), parse_addresses(cfg, [target])
    state = open_state(cfg)
    _top_level(state, ref)
    current = state.addresses(ref)
    if target not in current:
        raise TrackerError(f'{ref} is not published to {target} (addresses: {", ".join(current)})')
    if len(current) == 1:
        raise TrackerError(f'{target} is the last address of {ref}; to stop tracking: jira-relay rm {ref}')
    state.remove_address(ref, target)
    print(f'{ref}: no longer published to {target}; remaining: {", ".join(a for a in current if a != target)}')


def subtasks(cfg: config.Config, ref: str, on: bool, snapshot: str) -> None:
    """Turn picking up subtasks of an already tracked issue on or off.

    The poll only sees subtasks updated within the window, so turning it on sends snapshots of the
    existing subtasks right away, as add --with-subtasks does.
    """
    ref = parse_ref(cfg, ref)
    state = open_state(cfg)
    row = state.tracked(ref)
    if not row:
        raise TrackerError(f'{ref} is not tracked: jira-relay add {ref} <address> --with-subtasks')
    if row['parent_key']:
        raise TrackerError(f'{ref} is a subtask of {row["parent_key"]}; subtasks have no subtasks of their own')

    if not on:
        if snapshot != 'dry':
            state.set_with_subtasks(ref, False)
        print(f'{ref}: new subtasks are no longer picked up; already tracked ones stay (to remove: jira-relay rm <issue>)')
        return

    site, _ = split(ref)
    jira = cfg.jira(site)
    with tempfile.TemporaryDirectory() as tmp:
        refs = publish_snapshots(cfg, state, jira, site, _subtasks_in_jira(jira, state, site, ref),
                                 state.addresses(ref), True, snapshot, Path(tmp))
    if snapshot != 'dry':
        state.set_with_subtasks(ref, True)
        print(f'{ref}: subtasks are picked up → {", ".join(state.addresses(ref))}; '
              + (f'now tracked: {", ".join(refs)}' if refs else 'no subtasks yet'))


def untrack(cfg: config.Config, ref: str) -> None:
    ref = parse_ref(cfg, ref)
    removed = open_state(cfg).untrack(ref)
    if not removed:
        raise TrackerError(f'{ref} is not tracked')
    print(f'no longer tracked: {", ".join(removed)}')


def listing(cfg: config.Config) -> None:
    """Tracked issues, their addresses and who watches at each (in parentheses; none: added on the host)."""
    state = open_state(cfg)
    shown = set()
    for r in state.all_tracked():
        if r['parent_key']:
            if r['parent_key'] in shown:
                print(f'  {r["key"]:<16} subtask of {r["parent_key"]}, {r["added_at"]}')
            continue
        shown.add(r['key'])
        flags = ', with subtasks' if r['with_subtasks'] else ''
        targets = [f'{a} ({", ".join(by)})' if by else a for a, by in state.owners(r['key']).items()]
        print(f'{r["key"]:<18} {r["added_at"]}{flags} → {", ".join(targets)}')
    if not shown:
        print('nothing is tracked')


def claim(cfg: config.Config, login: str) -> None:
    """Addresses without an owner (added on the host, or by the admin's agents before 0.6) get this owner."""
    state = open_state(cfg)
    orphans = state.ownerless()
    for key, target in orphans:
        state.add_address(key, target, login)
    print(f'{login} now watches: ' + (', '.join(f'{k} → {a}' for k, a in orphans) if orphans else 'nothing new'))


def watch(cfg: config.Config, ref: str, targets: list[str], with_subtasks: bool, snapshot: str, owner: Owner) -> None:
    """A new issue is like add; an already tracked one gets the missing addresses and subtasks (subtasks on). At an
    address the issue already goes to, the owner only joins the watchers: no second snapshot."""
    ref = parse_ref(cfg, ref)
    state = open_state(cfg)
    row = state.tracked(ref)
    if not row:
        add(cfg, ref, targets, with_subtasks, snapshot, owner)
        return
    if row['parent_key']:
        raise TrackerError(f'{ref} is a subtask of {row["parent_key"]} and is tracked with it; '
                           f'addresses are set on {row["parent_key"]}')
    current = state.addresses(ref)
    wanted = parse_addresses(cfg, targets)
    new = [t for t in wanted if t not in current]
    joined = [t for t in wanted if t in current and owner and not state.is_owner(ref, t, owner[0])]
    turn_on = with_subtasks and not row['with_subtasks']
    if turn_on:
        subtasks(cfg, ref, True, snapshot)
    for target in new:
        add_address(cfg, ref, target, snapshot, owner)
    for target in joined:
        others = ', '.join(state.owners(ref)[target]) or 'the host'
        if snapshot != 'dry':
            state.add_address(ref, target, *owner)
        print(f'{ref} already goes to {target} (watched by {others}): you watch it there too, no new snapshot')
    if not (new or joined or turn_on):
        print(f'{ref} is already published to {", ".join(current)}')


def unwatch(cfg: config.Config, ref: str, target: str | None, login: str = '') -> None:
    """A host command (no login) removes the address (the last one untracks the issue), or the issue entirely.
    A user leaves the watchers of the address (all of their addresses without one); an address without watchers
    is removed, the issue without addresses untracked."""
    ref = parse_ref(cfg, ref)
    state = open_state(cfg)
    row = state.tracked(ref)
    if not row:
        raise TrackerError(f'{ref} is not tracked')
    if row['parent_key']:
        if target:
            raise TrackerError(f'{ref} is a subtask of {row["parent_key"]}: addresses are set on the parent')
        untrack(cfg, ref)
        return
    current = state.addresses(ref)
    if target:
        (target,) = parse_addresses(cfg, [target])
    if not login:
        if target and current != [target]:
            remove_address(cfg, ref, target)
        else:
            untrack(cfg, ref)
        return
    mine = [a for a in ([target] if target else current) if state.is_owner(ref, a, login)]
    if not mine:
        raise TrackerError(f'{ref}: {login} is not among the watchers' + (f' at {target}' if target else '')
                           + f' (jira watching shows who is)')
    for one in mine:
        if state.remove_owner(ref, one, login):
            print(f'{ref}: you no longer watch at {one}; it still goes there for {", ".join(state.owners(ref)[one])}')
            continue
        if state.addresses(ref) == [one]:
            untrack(cfg, ref)
            return
        state.remove_address(ref, one)
        print(f'{ref}: no longer published to {one}; remaining: {", ".join(state.addresses(ref))}')


def setup(cfg: config.Config) -> None:
    """Connect every outbound (service start, after `outbound add`): warnings go to the log, nothing fails."""
    for name in cfg.outbounds:
        try:
            warnings = outbounds.driver(cfg, name).setup()
        except (outbounds.OutboundError, hub.HubError) as e:
            warnings = [str(e)]
        for warning in warnings:
            log(f'setup: {warning}')


def poll_once(cfg: config.Config, dry_run: bool) -> int:
    if not dry_run:
        for name in cfg.outbounds:  # an unreachable outbound is no reason to skip the cycle: notes go with the next sync
            try:
                if warning := outbounds.driver(cfg, name).sync():
                    log(f'sync {name}: {warning}')
            except (outbounds.OutboundError, hub.HubError) as e:
                log(f'sync {name}: {e}')
    state, post, failed = open_state(cfg), outbounds.poster(cfg), 0
    for site in cfg.sites:  # one site failing (network, token) does not stop the others
        try:
            failed |= Poller(cfg, state, cfg.jira(site), post, dry_run=dry_run, site=site).run()
        except (config.ConfigError, JiraError) as e:
            log(f'{site}: cycle failed: {e}')
            failed = 1
    return failed
