"""Agent commands in Jira: request parsing, permissions, Jira calls, journal. Entry point: gateway/service.py.

The request comes from bin/jira on a PC over ssh:
    {"as": "backend", "argv": ["comment", "DEMO-42"], "text": "…", "files": {"a.png": "<base64>"}}
Reply: {"ok": true, "out": "…"} or {"ok": false, "error": "…"}; both texts go through mask().

Users are `users` in the jira-relay config file (config.py). The login is the account on the service host and the Jira login: bin/jira
goes over ssh as that account and passes `--user "$(id -un)"`, evaluated on the host. The admin uses the service
secret of each Jira site; others put their own token per site (`jira token set --jira <site>`), and actions come from
their account.

Several Jira sites: an issue is "<site>/<KEY>" or a bare key. A bare key (and a command without an issue: search, api,
create with a bare project, token) is on the site from `--jira NAME` (anywhere in argv), else on the agent's site if
all of its projects name one site, else on the default site (the first configured).

Any agent of a user can read and watch under its project name. Changes are allowed only to agents from
users.<login>.agents and only in their projects ("*", "<site>/*", "<site>/<PROJECT>"), signed "— agent <name>" wherever
there is text, and journaled (gateway/journal.py; a non-admin agent is "<login>/<name>", the target is "<site>/<KEY>").
Watching goes through tracker/api.py: any issue, the snapshot is sent at once (the user's command to the agent is the
confirmation), to the user's outbounds (config.outbounds_of; the admin — all). Polling and snapshots
use the service secret. Changing and watching happen on the user's command: that is an agent rule
(client/skills/jira/SKILL.md) plus Claude Code's confirmation; the service cannot check it.
"""
import argparse
import base64
import io
import json
import re
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

from .. import config, render
from ..config import Agent, Config
from ..issue import normalize
from ..jira import Jira, JiraError, make_auth
from ..mask import mask
from ..tracker.api import Tracker, TrackerError, hub_of
from . import AgentError
from .journal import Journal

KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*-\d+$')
PROJECT_RE = re.compile(r'^[A-Z][A-Z0-9_]*$')
NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')  # agent name is its project name, as in xchg
SUMMARY_LIMIT = 200  # how much request text the journal keeps

# api: anything under these prefixes can be read, only the listed paths changed, DELETE never
API_PREFIXES = ('/rest/api/2/', '/rest/agile/1.0/')
API_WRITABLE = re.compile(
    r'^/rest/api/2/(issue(/.*)?|issueLink|search|version(/.*)?|component(/.*)?|filter(/.*)?)$'
    r'|^/rest/agile/1\.0/(issue|sprint|backlog|epic)(/.*)?$'
)
KEY_IN_PATH = re.compile(r'/issue/([A-Z][A-Z0-9_]*-\d+)(?:/|$)')
COMMENT_PATH = re.compile(r'^/rest/api/2/issue/[^/]+/comment(/\d+)?$')

# edit: how a field looks in Jira JSON
NAMED = {'priority', 'assignee', 'reporter'}               # {"name": v}
STRINGS = {'labels'}                                       # ["a", "b"]
NAMED_LISTS = {'components', 'fixVersions', 'versions'}    # [{"name": v}]
EDIT_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_.]*)(\+=|-=|:=|=)(.*)$', re.S)


class _HelpShown(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that never exits the process: errors raise, help goes to a captured stdout."""

    def error(self, message):
        raise AgentError(f'{self.prog}: {message}')

    def exit(self, status=0, message=None):
        raise _HelpShown


@dataclass
class Context:
    cfg: Config
    login: str  # the account on the service host and the Jira login
    journal: Journal
    agent: str
    projects: tuple[str, ...]  # where the agent may change Jira; empty: not in the user's config
    site: str  # for bare keys and commands without an issue
    client: Callable[[str], Jira | None]  # site → the user's Jira client; None: no token for that site
    hubs: tuple[str, ...] | None = None  # outbounds the user may watch to; None — all (the admin)
    watch_to: str = ''
    make_jira: object = Jira  # (url, auth) → Jira: checks a new token
    text: str = ''
    files: dict[str, bytes] = field(default_factory=dict)
    tracker: Tracker | None = None  # without it watch is unavailable
    secrets: list[str] = field(default_factory=list)  # never echoed: tokens of the clients used
    clients: dict[str, Jira] = field(default_factory=dict)
    # what to journal: set by the change handler before calling Jira
    target: str = ''
    summary: str = ''

    @property
    def admin(self) -> bool:
        return self.login == self.cfg.admin

    def jira(self, site: str) -> Jira:
        if site not in self.clients:
            try:
                client = self.client(site)
            except config.ConfigError as e:
                raise AgentError(str(e)) from None
            if client is None:
                raise AgentError(f'{self.login} has no Jira token for {site}; put yours — jira token set --jira {site} '
                                 f'< file with the PAT (Jira → Profile → Personal Access Tokens)')
            self.secrets += client.auth.secrets()
            self.clients[site] = client
        return self.clients[site]

    def url(self, site: str) -> str:
        return self.cfg.sites[site].url


def signed(text: str, agent: str) -> str:
    signature = f'_— agent {agent}_'  # Jira Server wiki markup: italics
    return f'{text.rstrip()}\n\n{signature}' if text.strip() else signature


def check_site(cfg: Config, name: str) -> str:
    try:
        return cfg.site(name).name
    except config.ConfigError as e:
        raise AgentError(str(e)) from None


def default_site(cfg: Config, agent: Agent, chosen: str = '') -> str:
    """--jira, else the agent's site when all of its projects name one, else the first configured site."""
    if chosen:
        return check_site(cfg, chosen)
    sites = {p.partition('/')[0] for p in agent.projects}
    if len(sites) == 1 and all('/' in p for p in agent.projects):
        return next(iter(sites))
    try:
        return cfg.default_site
    except config.ConfigError as e:
        raise AgentError(str(e)) from None


def split_site(ctx: Context, value: str, site: str | None = None) -> tuple[str, str]:
    """"main/X" → ("main", "X"); a bare "X" is on site (default: ctx.site). The site is checked."""
    name, _, rest = value.strip().rpartition('/')
    return check_site(ctx.cfg, name or site or ctx.site), rest


def parse_key(ctx: Context, value: str, site: str | None = None) -> tuple[str, str]:
    """Issue "main/DEMO-1" or "DEMO-1" → (site, KEY)."""
    site, key = split_site(ctx, value, site)
    key = key.upper()
    if not KEY_RE.match(key):
        raise AgentError(f'"{value}" does not look like a Jira issue key (e.g. DEMO-42 or {ctx.site}/DEMO-42)')
    return site, key


def who(ctx: Context) -> str:
    """Name in the journal and in "who added the address": admin's agents as is, others prefixed with the login."""
    return ctx.agent if ctx.admin else f'{ctx.login}/{ctx.agent}'


def require_hubs(ctx: Context, targets: list[str]) -> None:
    if ctx.hubs is None:
        return
    for target in targets:
        if hub_of(target.strip()) not in ctx.hubs:
            raise AgentError(f'"{target}": user {ctx.login} can watch only in hubs '
                             f'{", ".join(ctx.hubs) or "— none"} (users.{ctx.login}.outbounds in the jira-relay config)')


def may_write(projects: tuple[str, ...], site: str, project: str) -> bool:
    for entry in projects:
        if entry == '*':
            return True
        name, _, allowed = entry.partition('/')
        if name == site and (allowed == '*' or allowed.upper() == project.upper()):
            return True
    return False


def require_write(ctx: Context, site: str, *targets: str) -> None:
    """Changes are allowed only in the agent's projects; target is an issue or project key on site."""
    if not ctx.projects:
        raise AgentError(f'agent {who(ctx)} cannot change Jira: it is not among the agents of {ctx.login} in the '
                         f'jira-relay config. Reading and watching are allowed; for changes ask the jira-relay admin '
                         f'to add the agent and its Jira projects')
    for target in targets:
        if not may_write(ctx.projects, site, target.split('-')[0]):
            raise AgentError(f'agent {who(ctx)} cannot change {site}/{target}: its projects are '
                             f'{", ".join(ctx.projects)} (users.{ctx.login}.agents.{ctx.agent} in the jira-relay config)')


def need_text(ctx: Context, what: str) -> str:
    if not ctx.text.strip():
        raise AgentError(f'no text: pass the {what} on stdin (heredoc)')
    return ctx.text


def summarize(text: str) -> str:
    line = ' '.join(text.split())
    return line if len(line) <= SUMMARY_LIMIT else line[:SUMMARY_LIMIT - 1] + '…'


def comment_url(base_url: str, key: str, comment_id) -> str:
    return f'{render.browse_url(base_url, key)}?focusedCommentId={comment_id}#comment-{comment_id}'


def add_comment(ctx: Context, site: str, key: str, text: str) -> str:
    reply = ctx.jira(site).write('POST', f'/rest/api/2/issue/{quote(key)}/comment', {'body': signed(text, ctx.agent)})
    return comment_url(ctx.url(site), key, reply['id'])


# --- reads -----------------------------------------------------------------------------------------
def cmd_get(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    raw = ctx.jira(site).issue(key)
    if args.json:
        return json.dumps(raw, ensure_ascii=False, indent=1)
    return render.snapshot(normalize(raw), ctx.url(site), ctx.cfg.tz, with_subtasks=None)


def cmd_search(ctx: Context, args) -> str:
    page = ctx.jira(ctx.site).get('/rest/api/2/search', {
        'jql': args.jql, 'maxResults': args.limit, 'fields': 'summary,status,issuetype,assignee,updated',
    })
    if args.json:
        return json.dumps(page, ensure_ascii=False, indent=1)
    lines = []
    for issue in page.get('issues') or []:
        f = issue['fields']
        assignee = (f.get('assignee') or {}).get('displayName') or '—'
        lines.append(f'{issue["key"]:<11} {(f.get("status") or {}).get("name", ""):<14} '
                     f'{(f.get("issuetype") or {}).get("name", "")} · {f.get("summary", "")} · {assignee}')
    lines.append(f'found {page.get("total", len(lines))}, shown {len(page.get("issues") or [])}')
    return '\n'.join(lines)


def cmd_transitions(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    data = ctx.jira(site).get(f'/rest/api/2/issue/{quote(key)}/transitions')
    lines = [f'{t["name"]} → {t["to"]["name"]}' for t in data.get('transitions') or []]
    return '\n'.join(lines) or f'{key}: no transitions'


def cmd_log(ctx: Context, args) -> str:
    since = datetime.now(timezone.utc) - parse_period(args.since)
    rows = ctx.journal.since(since.strftime('%Y-%m-%dT%H:%M:%SZ'), None if args.all else who(ctx))
    lines = [f'{r["at"]}  {r["agent"]:<10} {r["op"]:<10} {r["target"]:<17} {r["result"]}'
             + (f'\n    {r["summary"]}' if r['summary'] else '') for r in rows]
    return '\n'.join(lines) or f'no actions in the last {args.since}'


def parse_period(value: str) -> timedelta:
    match = re.fullmatch(r'(\d+)([dhm])', value.strip())
    if not match:
        raise AgentError(f'"{value}" is not a period: 7d, 24h, 30m')
    n, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{'d': 'days', 'h': 'hours', 'm': 'minutes'}[unit]: n})


# --- changes -------------------------------------------------------------------------------------
def cmd_comment(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    require_write(ctx, site, key)
    text = need_text(ctx, 'comment')
    ctx.target, ctx.summary = f'{site}/{key}', summarize(text)
    return add_comment(ctx, site, key, text)


def cmd_create(ctx: Context, args) -> str:
    """The project is DEMO or main/DEMO; a bare --parent is on the project's site."""
    site, project = split_site(ctx, args.project)
    project = project.upper()
    if not PROJECT_RE.match(project):
        raise AgentError(f'"{args.project}" does not look like a Jira project key (e.g. DEMO or {ctx.site}/DEMO)')
    parent = None
    if args.parent:
        parent_site, parent = parse_key(ctx, args.parent, site)
        if parent_site != site:
            raise AgentError(f'--parent {args.parent} is on {parent_site}, the project on {site}')
    require_write(ctx, site, project, *([parent] if parent else []))
    fields = {
        'project': {'key': project}, 'issuetype': {'name': args.type}, 'summary': args.summary,
        'description': signed(ctx.text, ctx.agent),
    }
    if parent:
        fields['parent'] = {'key': parent}
    if args.assignee:
        fields['assignee'] = {'name': args.assignee}
    if args.priority:
        fields['priority'] = {'name': args.priority}
    if args.label:
        fields['labels'] = args.label
    ctx.target, ctx.summary = f'{site}/{project}', summarize(f'{args.type}: {args.summary}')
    reply = ctx.jira(site).write('POST', '/rest/api/2/issue', {'fields': fields})
    ctx.target = f'{site}/{reply["key"]}'
    return f'{reply["key"]} {render.browse_url(ctx.url(site), reply["key"])}'


def cmd_transition(ctx: Context, args) -> str:
    """Transition by transition name or target status. The comment is a separate request: Jira rejects the comment
    field in a transition without a screen."""
    site, key = parse_key(ctx, args.key)
    require_write(ctx, site, key)
    text = need_text(ctx, 'transition comment') if args.comment else ''
    wanted = args.name.casefold()
    jira = ctx.jira(site)
    available = jira.get(f'/rest/api/2/issue/{quote(key)}/transitions').get('transitions') or []
    found = {t['id']: t for t in available if wanted in (t['name'].casefold(), t['to']['name'].casefold())}
    if len(found) != 1:
        listed = '; '.join(f'{t["name"]} → {t["to"]["name"]}' for t in available) or 'none'
        problem = 'ambiguous' if found else 'no such transition'
        raise AgentError(f'{key}: "{args.name}" — {problem}. Available: {listed}')
    (transition,) = found.values()
    ctx.target = f'{site}/{key}'
    ctx.summary = summarize(f'→ {transition["to"]["name"]}' + (f'; {text}' if text else ''))
    jira.write('POST', f'/rest/api/2/issue/{quote(key)}/transitions', {'transition': {'id': transition['id']}})
    out = f'{key}: {transition["to"]["name"]}'
    if text:
        out += f'\n{add_comment(ctx, site, key, text)}'
    return out


def cmd_assign(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    require_write(ctx, site, key)
    login = None if args.login == '-' else args.login
    ctx.target, ctx.summary = f'{site}/{key}', f'assignee → {login or "nobody"}'
    ctx.jira(site).write('PUT', f'/rest/api/2/issue/{quote(key)}/assignee', {'name': login})
    return f'{key}: assignee {login or "removed"}'


def edit_body(changes: list[str]) -> dict:
    """field=value sets, field+=v / field-=v adds to / removes from a list, field:=<json> passes as is."""
    fields, update = {}, {}
    for change in changes:
        match = EDIT_RE.match(change)
        if not match:
            raise AgentError(f'"{change}": expected field=value, field+=value, field-=value or field:=<json>')
        name, op, value = match.groups()
        if op == ':=':
            try:
                fields[name] = json.loads(value)
            except ValueError as e:
                raise AgentError(f'{name}: not JSON — {e}') from None
        elif op == '=':
            if name in NAMED:
                fields[name] = {'name': value} if value else None
            elif name in STRINGS:
                fields[name] = [v.strip() for v in value.split(',') if v.strip()]
            elif name in NAMED_LISTS:
                fields[name] = [{'name': v.strip()} for v in value.split(',') if v.strip()]
            else:
                fields[name] = value
        else:
            verb = 'add' if op == '+=' else 'remove'
            if name in STRINGS:
                update.setdefault(name, []).append({verb: value})
            elif name in NAMED_LISTS:
                update.setdefault(name, []).append({verb: {'name': value}})
            else:
                raise AgentError(f'{name}{op}: adding and removing work only for lists '
                                 f'({", ".join(sorted(STRINGS | NAMED_LISTS))})')
    body = {}
    if fields:
        body['fields'] = fields
    if update:
        body['update'] = update
    return body


def cmd_edit(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    require_write(ctx, site, key)
    body = edit_body(args.changes)
    ctx.target, ctx.summary = f'{site}/{key}', summarize(' '.join(args.changes))
    ctx.jira(site).write('PUT', f'/rest/api/2/issue/{quote(key)}', body)
    return f'{key}: changed {", ".join(sorted(set(body.get("fields", {})) | set(body.get("update", {}))))}'


def link_direction(types: list[dict], phrase: str, key: str, other: str) -> tuple[str, str, str]:
    """(type, source, target) for "key <phrase> other". The phrase is the type name, its outward ("blocks") or inward
    ("is blocked by") phrase. In POST issueLink the source is inwardIssue: Jira shows the outward phrase on it."""
    wanted = phrase.casefold()
    for t in types:
        if wanted in (t['name'].casefold(), t['outward'].casefold()):
            return t['name'], key, other
        if wanted == t['inward'].casefold():
            return t['name'], other, key
    listed = '; '.join(f'{t["name"]}: "{t["outward"]}" / "{t["inward"]}"' for t in types)
    raise AgentError(f'no link type "{phrase}". Available: {listed}')


def cmd_link(ctx: Context, args) -> str:
    """Both issues on one site; a bare second key is on the first one's site."""
    site, key = parse_key(ctx, args.key)
    other_site, other = parse_key(ctx, args.other, site)
    if other_site != site:
        raise AgentError(f'link: {site}/{key} and {other_site}/{other} are on different Jira sites; '
                         f'Jira links issues of one site only')
    require_write(ctx, site, key)
    jira = ctx.jira(site)
    types = jira.get('/rest/api/2/issueLinkType').get('issueLinkTypes') or []
    name, source, target = link_direction(types, args.type, key, other)
    ctx.target, ctx.summary = f'{site}/{key}', f'{args.type} {other}'
    jira.write('POST', '/rest/api/2/issueLink', {
        'type': {'name': name}, 'inwardIssue': {'key': source}, 'outwardIssue': {'key': target},
    })
    # show the link the way Jira now sees it: the direction is easy to get wrong
    links = (jira.get(f'/rest/api/2/issue/{quote(key)}', {'fields': 'issuelinks'})['fields'].get('issuelinks') or [])
    for link in links:
        for side, phrase in (('outwardIssue', 'outward'), ('inwardIssue', 'inward')):
            if (link.get(side) or {}).get('key') == other and link['type']['name'] == name:
                return f'{key} {link["type"][phrase]} {other}'
    return f'{key} — {other}: link created ({name})'


def multipart(files: dict[str, bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, content in files.items():
        safe = name.replace('"', '_').replace('\r', '_').replace('\n', '_')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b'\r\n')
    return b''.join(parts) + f'--{boundary}--\r\n'.encode(), f'multipart/form-data; boundary={boundary}'


def cmd_attach(ctx: Context, args) -> str:
    site, key = parse_key(ctx, args.key)
    require_write(ctx, site, key)
    missing = [n for n in args.files if n not in ctx.files]
    if missing:
        raise AgentError(f'files did not arrive: {", ".join(missing)}')
    limit = ctx.cfg.max_attachment
    big = [f'{n} ({render.human_size(len(ctx.files[n]))})' for n in args.files if len(ctx.files[n]) > limit]
    if big:
        raise AgentError(f'larger than {render.human_size(limit)}: {", ".join(big)}')
    ctx.target, ctx.summary = f'{site}/{key}', ', '.join(args.files)
    data, content_type = multipart({n: ctx.files[n] for n in args.files})
    reply = ctx.jira(site).write('POST', f'/rest/api/2/issue/{quote(key)}/attachments', data=data,
                                 content_type=content_type)
    return '\n'.join(f'{a["filename"]} {a["content"]}' for a in reply or [])


def api_targets(path: str, body) -> list[str]:
    """What an api request changes: issue keys from the path and body, the project of a new issue or version."""
    targets = KEY_IN_PATH.findall(path)
    if isinstance(body, dict):
        fields = body.get('fields') or {}
        if isinstance(fields.get('project'), dict) and fields['project'].get('key'):
            targets.append(fields['project']['key'])
        for side in ('inwardIssue', 'outwardIssue'):
            if isinstance(body.get(side), dict) and body[side].get('key'):
                targets.append(body[side]['key'])
        if isinstance(body.get('project'), str):
            targets.append(body['project'])
        for issue in body.get('issueUpdates') or []:  # POST issue/bulk
            targets += api_targets('', issue)
    return targets


def cmd_api(ctx: Context, args) -> str:
    """On the default site (or --jira): paths and bodies carry bare Jira keys."""
    method = args.method.upper()
    path, _, query = args.path.partition('?')
    if not path.startswith(API_PREFIXES):
        raise AgentError(f'api: only {" and ".join(API_PREFIXES)}…')
    params = dict(parse_qsl(query))
    site = ctx.site
    if method == 'GET':
        return json.dumps(ctx.jira(site).get(path, params), ensure_ascii=False, indent=1)
    if method not in ('POST', 'PUT'):
        raise AgentError(f'api: {method} is not allowed — only the jira-relay admin deletes in Jira, by hand')
    if not API_WRITABLE.match(path):
        raise AgentError(f'api: {method} {path} — not issues, links or sprints; only the jira-relay admin changes that')
    try:
        body = json.loads(ctx.text) if ctx.text.strip() else None
    except ValueError as e:
        raise AgentError(f'api: body on stdin is not JSON: {e}') from None
    if method == 'POST' and path == '/rest/api/2/search':  # search via POST is a read
        return json.dumps(ctx.jira(site).write(method, path, body), ensure_ascii=False, indent=1)

    targets = api_targets(path, body)
    if not targets and not may_write(ctx.projects, site, '*'):
        raise AgentError('api: cannot tell which issue or project the request changes — '
                         'give the issue by key in the path (/issue/DEMO-1/…), not by numeric id')
    require_write(ctx, site, *targets)
    if isinstance(body, dict):
        if COMMENT_PATH.match(path) and isinstance(body.get('body'), str):
            body['body'] = signed(body['body'], ctx.agent)
        elif path == '/rest/api/2/issue' and method == 'POST' and isinstance(body.get('fields'), dict):
            body['fields']['description'] = signed(body['fields'].get('description') or '', ctx.agent)
    ctx.target = f'{site}/{targets[0]}' if targets else site
    ctx.summary = summarize(f'{method} {args.path} {ctx.text}')
    reply = ctx.jira(site).write(method, path + (f'?{query}' if query else ''), body)
    return json.dumps(reply, ensure_ascii=False, indent=1) if reply is not None else 'ok'


# --- watching: through the tracker (tracker/api.py) --------------------------------------------------
def tracker(ctx: Context) -> Tracker:
    if ctx.tracker is None:
        raise AgentError('watching is unavailable: the service runs without settings')
    return ctx.tracker


def snapshot_mode(args) -> str:
    return 'dry' if args.dry_run else 'none' if args.no_snapshot else 'send'


def tracked(ctx: Context, call) -> str:
    try:
        return call(tracker(ctx))
    except TrackerError as e:
        raise AgentError(str(e)) from None


def watched_ref(ctx: Context, value: str) -> str:
    """"<site>/<KEY>" for the tracker. The user needs their own token for the site: the tracker reads Jira with the
    service secret, and a user without access to the site must not get its issues through watching."""
    site, key = parse_key(ctx, value)
    ctx.jira(site)
    return f'{site}/{key}'


def cmd_watch(ctx: Context, args) -> str:
    ref = watched_ref(ctx, args.key)
    targets = args.addresses or ([ctx.watch_to] if ctx.watch_to else [])
    if not targets:
        raise AgentError(f'where to send updates? Give an address (work:@project) — agent {who(ctx)} has no watch_to '
                         f'in the jira-relay config')
    require_hubs(ctx, targets)
    if not args.dry_run:
        ctx.target, ctx.summary = ref, ' '.join(targets) + (' +subtasks' if args.with_subtasks else '')
    return tracked(ctx, lambda t: t.watch(ref, targets, with_subtasks=args.with_subtasks,
                                          snapshot=snapshot_mode(args), by=who(ctx)))


def cmd_unwatch(ctx: Context, args) -> str:
    ref = watched_ref(ctx, args.key)
    if args.address:
        require_hubs(ctx, [args.address])
    ctx.target, ctx.summary = ref, args.address or 'all addresses'
    # without an address, only own hubs: other users' addresses stay
    return tracked(ctx, lambda t: t.unwatch(ref, args.address, hubs=ctx.hubs))


def cmd_subtasks(ctx: Context, args) -> str:
    ref = watched_ref(ctx, args.key)
    if not args.dry_run:
        ctx.target, ctx.summary = ref, f'subtasks {args.mode}'
    return tracked(ctx, lambda t: t.subtasks(ref, args.mode == 'on', snapshot=snapshot_mode(args)))


def cmd_watching(ctx: Context, args) -> str:
    return tracked(ctx, lambda t: t.watching(ctx.hubs))


def cmd_sites(ctx: Context, args) -> str:
    """What the agent should know before working: Jira sites, its own rights and where it may watch to."""
    lines = ['Jira sites (a bare issue key goes to the one marked *):']
    for name, site in ctx.cfg.sites.items():
        token = ('service secret' if ctx.admin else
                 'your token set' if ctx.cfg.user_secret_path(ctx.login, name).exists() else
                 f'no token yet, set it with jira token set --jira {name}')
        lines.append(f'  {"*" if name == ctx.site else " "} {name:<12} {site.url}  ({token})')
    lines.append(f'you: {ctx.login}' + (' (the admin)' if ctx.admin else ''))
    lines.append(f'agent {ctx.agent} may change: ' + (', '.join(ctx.projects) if ctx.projects else
                                                     'nothing (read and watch only)'))
    hubs = ctx.cfg.hubs if ctx.hubs is None else ctx.hubs
    lines.append('watch to: ' + (', '.join(f'{h}:…' for h in hubs) or 'nowhere')
                 + (f'; default {ctx.watch_to}' if ctx.watch_to else ''))
    return '\n'.join(lines)


# --- user token -------------------------------------------------------------------------------------
def cmd_token(ctx: Context, args) -> str:
    """The user's own token for one site (ctx.site: --jira or the default)."""
    site = ctx.cfg.sites[ctx.site]
    if ctx.admin:
        raise AgentError(f'the admin uses the service secret of {site.name} (jira-relay jira secret {site.name} on the service host); '
                         f'it is not changed through an agent')
    path = ctx.cfg.user_secret_path(ctx.login, site.name)
    if args.action == 'set':
        token = ctx.text.strip()
        if not token or len(token.split()) != 1:
            raise AgentError(f'the token is one line on stdin: jira token set --jira {site.name} < file '
                             f'(or paste it when prompted)')
        try:
            login = ctx.make_jira(site.url, make_auth(site.auth, token)).whoami()
        except JiraError as e:
            raise AgentError(f'{site.name} rejected the token ({e})') from None
        if login.casefold() != ctx.login.casefold():
            raise AgentError(f'the token belongs to {site.name} user "{login}", but you are registered as "{ctx.login}"')
        config.write_secret(path, token)
        return f'token for {site.name} saved; Jira actions there come from {login}'
    if args.action == 'rm':
        try:
            path.unlink()
        except FileNotFoundError:
            return f'there was no token for {site.name}'
        return f'token for {site.name} removed; revoke it in Jira: Profile → Personal Access Tokens'
    if path.exists():
        return f'token for {site.name} is set; Jira acts as {ctx.login} there'
    return f'no token for {site.name} yet; run jira token set --jira {site.name}'


READS = {'get', 'search', 'transitions', 'log', 'watching', 'token', 'sites'}


def build_parser() -> _Parser:
    parser = _Parser(prog='jira --as <agent>', description='Jira through jira-relay: read freely, change only on your '
                                              "human's command. Text (comment, description, api body) goes on stdin. "
                                              'An issue is DEMO-42 or <site>/DEMO-42.')
    parser.add_argument('--jira', metavar='SITE', help='Jira site for bare keys and commands without an issue; '
                                                       'accepted anywhere in the command')
    sub = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)

    p = sub.add_parser('get', help='the whole issue: fields, description, comments, links, subtasks')
    p.add_argument('key')
    p.add_argument('--json', action='store_true', help='raw Jira JSON')
    p.set_defaults(handler=cmd_get)

    p = sub.add_parser('search', help="JQL search: 'project = DEMO AND status = Review'")
    p.add_argument('jql')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser('transitions', help='transitions available for the issue')
    p.add_argument('key')
    p.set_defaults(handler=cmd_transitions)

    p = sub.add_parser('log', help='journal of changes made through the service')
    p.add_argument('--since', default='7d', help='7d, 24h, 30m')
    p.add_argument('--all', action='store_true', help='all agents, not just this one')
    p.set_defaults(handler=cmd_log)

    p = sub.add_parser('comment', help='comment (text on stdin); the service adds the agent signature')
    p.add_argument('key')
    p.set_defaults(handler=cmd_comment)

    p = sub.add_parser('create', help='issue: DEMO (or <site>/DEMO) --type Task --summary … (description on stdin)')
    p.add_argument('project')
    p.add_argument('--type', required=True, help='issue type as in Jira: Task, Bug, Sub-task, …')
    p.add_argument('--summary', required=True)
    p.add_argument('--parent', help='parent for a subtask')
    p.add_argument('--assignee', help='login')
    p.add_argument('--priority')
    p.add_argument('--label', action='append', help='label; repeatable')
    p.set_defaults(handler=cmd_create)

    p = sub.add_parser('transition', help="transition by transition or status name: DEMO-42 'Review'")
    p.add_argument('key')
    p.add_argument('name')
    p.add_argument('--comment', action='store_true', help='plus a comment from stdin')
    p.set_defaults(handler=cmd_transition)

    p = sub.add_parser('assign', help='assignee: login or - (remove)')
    p.add_argument('key')
    p.add_argument('login')
    p.set_defaults(handler=cmd_assign)

    p = sub.add_parser('edit', help='fields: priority=High labels+=x summary=… customfield_1:=<json>')
    p.add_argument('key')
    p.add_argument('changes', nargs='+')
    p.set_defaults(handler=cmd_edit)

    p = sub.add_parser('link', help="link: DEMO-42 'blocks' DEMO-43 (type name or its phrase; one site)")
    p.add_argument('key')
    p.add_argument('type')
    p.add_argument('other')
    p.set_defaults(handler=cmd_link)

    p = sub.add_parser('attach', help='attachments from the PC: DEMO-42 file.png …')
    p.add_argument('key')
    p.add_argument('files', nargs='+')
    p.set_defaults(handler=cmd_attach)

    p = sub.add_parser('watch', help='watch an issue: changes arrive as notes in a hub (default: the agent watch_to)')
    p.add_argument('key')
    p.add_argument('addresses', nargs='*', metavar='address', help='hub[:@project][:person]: team:@backend, me:alice')
    p.add_argument('--with-subtasks', action='store_true', help='and subtasks; new ones are picked up automatically')
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--no-snapshot', action='store_true', help='no snapshot: only changes from now on')
    choice.add_argument('--dry-run', action='store_true', help='show snapshots, send and store nothing')
    p.set_defaults(handler=cmd_watch)

    p = sub.add_parser('unwatch', help='stop watching: without an address, entirely (with subtasks)')
    p.add_argument('key')
    p.add_argument('address', nargs='?')
    p.set_defaults(handler=cmd_unwatch)

    p = sub.add_parser('subtasks', help='pick up subtasks of a watched issue: DEMO-42 on|off')
    p.add_argument('key')
    p.add_argument('mode', choices=('on', 'off'))
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--no-snapshot', action='store_true', help='do not send snapshots of existing subtasks')
    choice.add_argument('--dry-run', action='store_true')
    p.set_defaults(handler=cmd_subtasks)

    p = sub.add_parser('sites', help='Jira sites, your token per site, what this agent may change and watch to')
    p.set_defaults(handler=cmd_sites)

    p = sub.add_parser('watching', help='what is watched and where (in parentheses: the agent that added the address)')
    p.set_defaults(handler=cmd_watching)

    p = sub.add_parser('token', help='your own Jira token for a site (not for the admin): set (from stdin), status, rm; '
                                     '--jira SITE')
    p.add_argument('action', choices=('set', 'status', 'rm'))
    p.set_defaults(handler=cmd_token)

    p = sub.add_parser('api', help='any REST: GET|POST|PUT /rest/api/2/… (JSON body on stdin); --jira SITE')
    p.add_argument('method')
    p.add_argument('path')
    p.set_defaults(handler=cmd_api)
    return parser


def take_site(argv: list[str]) -> tuple[list[str], str]:
    """`--jira NAME` / `--jira=NAME` anywhere in argv (like --as in the client): (the rest, NAME)."""
    rest, site, i = [], '', 0
    while i < len(argv):
        if argv[i] == '--jira':
            if i + 1 >= len(argv) or argv[i + 1].startswith('-'):
                raise AgentError('--jira: the Jira site name is required')
            site, i = argv[i + 1], i + 2
            continue
        if argv[i].startswith('--jira='):
            site = argv[i].partition('=')[2]
            if not site:
                raise AgentError('--jira: the Jira site name is required')
        else:
            rest.append(argv[i])
        i += 1
    return rest, site


def handle(request: dict, *, cfg: Config, login: str, journal: Journal, tracker: Tracker | None = None,
           make_jira=Jira) -> dict:
    """One agent request of a registered user (login in cfg.users) → reply. Changes (failed ones too) are journaled.
    Jira clients come from cfg.user_jira(login, site); make_jira(url, auth) checks a new token."""
    parser = build_parser()
    help_out = io.StringIO()
    try:
        argv, chosen = take_site([str(a) for a in request.get('argv') or []])
        with redirect_stdout(help_out):
            args = parser.parse_args(argv)
    except _HelpShown:
        return {'ok': True, 'out': help_out.getvalue()}
    except AgentError as e:
        return {'ok': False, 'error': f'{e}\n\n{parser.format_usage()}'}

    secrets = []
    if args.command == 'token' and (request.get('text') or '').strip():
        secrets.append(request['text'].strip())  # the user's new token: never in replies or errors

    def clean(text: str) -> str:
        for secret in secrets + (ctx.secrets if ctx else []):
            if secret:
                text = text.replace(secret, '«hidden: token»')
        return mask(text)[0]

    ctx = None
    agent = request.get('as') or ''
    if not NAME_RE.match(agent):
        return {'ok': False, 'error': f'--as "{agent}": your project name is required ([a-z0-9._-]), e.g. --as backend'}
    info = cfg.users[login].agents.get(agent, Agent())  # not listed: read and watch only
    try:
        files = {n: base64.b64decode(v) for n, v in (request.get('files') or {}).items()}
    except (ValueError, TypeError) as e:
        return {'ok': False, 'error': f'cannot decode attachments: {e}'}
    try:
        site = default_site(cfg, info, chosen)
    except AgentError as e:
        return {'ok': False, 'error': clean(str(e))}
    ctx = Context(cfg=cfg, login=login, journal=journal, agent=agent, projects=info.projects, site=site,
                  client=lambda name: cfg.user_jira(login, name), hubs=cfg.outbounds_of(login),
                  watch_to=info.watch_to, make_jira=make_jira, text=request.get('text') or '', files=files,
                  tracker=tracker)

    write = args.command not in READS and not (args.command == 'api' and args.method.upper() == 'GET')
    try:
        out = args.handler(ctx, args)
    except (AgentError, JiraError) as e:
        if write and ctx.target:  # it reached Jira: journal the refusal too
            journal.add(who(ctx), args.command, ctx.target, ctx.summary, clean(str(e)))
        return {'ok': False, 'error': clean(str(e))}
    if write and ctx.target:
        journal.add(who(ctx), args.command, ctx.target, ctx.summary, clean(out.splitlines()[0] if out else 'ok'))
    return {'ok': True, 'out': clean(out)}
