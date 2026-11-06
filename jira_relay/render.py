"""Note texts for the hub. Schema: docs/design.md.

Text from Jira (description, comments) goes into a code block as is, so Jira wiki markup does not mix
with the note's markdown (e.g. a "# item" list entry does not become a heading).
"""
import difflib
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def text_diff(old: str, new: str) -> str:
    return '\n'.join(list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm='', n=3))[2:])


def browse_url(base_url: str, key: str) -> str:
    return f'{base_url.rstrip("/")}/browse/{key}'


def fmt_time(value: str | None, tz: str) -> str:
    if not value:
        return '—'
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%f%z').astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M')


def one_line(text: str | None) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def cell(text: str | None) -> str:
    return one_line(text).replace('|', '\\|') or '—'


def person(user: dict | None) -> str:
    return f'{user["display"]} ({user["name"]})' if user else '—'


def fence(text: str, lang: str = '') -> str:
    ticks = '`' * max(3, max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
    return f'{ticks}{lang}\n{text.rstrip()}\n{ticks}'


def plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def human_size(size: int) -> str:
    for unit in ('B', 'KB', 'MB'):
        if size < 1024 or unit == 'MB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return ''


ATTACHED = 'attached to the note'  # attachments.prepare mark for a file that went to the hub


def attachment_line(attachment: dict, tz: str, notes: dict[str, str] | None, prefix: str = '') -> str:
    """Attachment line. Without marks (`show`): just the Jira link; with a mark: what happened to the file."""
    head = (f'- {prefix}{attachment["filename"]} — {human_size(attachment["size"])}, {person(attachment["author"])}'
            f'{", " + fmt_time(attachment["created"], tz) if attachment.get("created") else ""}')
    note = (notes or {}).get(attachment['id'])
    if note is None:
        return f'{head} — {attachment["url"]}'
    if note == ATTACHED:
        return f'{head} — {ATTACHED}; in Jira: {attachment["url"]}'
    return f'{head} — {note}: {attachment["url"]}'


def brief(other: dict) -> str:
    status = f' — {other["status"]}' if other.get('status') else ''
    return f'{other["key"]} «{one_line(other["summary"])}»{status}'


def snapshot(issue: dict, base_url: str, tz: str, with_subtasks: bool | None, notes: dict[str, str] | None = None) -> str:
    rows = [
        ('Link', browse_url(base_url, issue['key'])),
        ('Type / status', f'{issue["type"]} / {issue["status"]} (resolution: {issue["resolution"] or "—"})'),
        ('Priority', issue['priority']),
        ('Assignee', person(issue['assignee'])),
        ('Reporter', person(issue['reporter'])),
        ('Fix versions', ', '.join(issue['fix_versions'])),
    ]
    if issue['parent']:
        rows.append(('Parent', brief(issue['parent'])))
    rows.append(('Created / updated', f'{fmt_time(issue["created"], tz)} / {fmt_time(issue["updated"], tz)} ({tz})'))
    if with_subtasks is not None:  # None: not for the hub (agent's jira get), say nothing about tracking
        rows.append(('Tracking', 'with subtasks' if with_subtasks else 'without subtasks'))
    out = [f'# {issue["key"]} · snapshot: {one_line(issue["summary"])}', '', '| | |', '|---|---|']
    out += [f'| {name} | {cell(value)} |' for name, value in rows]

    out += ['', '## Description', fence(issue['description']) if issue['description'].strip() else '—']
    if issue['subtasks']:
        out += ['', '## Subtasks'] + [f'- {brief(s)}' for s in issue['subtasks']]
    if issue['links']:
        out += ['', '## Links'] + [f'- {link["phrase"]} {brief(link)}' for link in issue['links']]
    if issue['attachments']:
        out += ['', '## Attachments'] + [attachment_line(a, tz, notes) for a in issue['attachments']]
    out += ['', f'## Comments ({len(issue["comments"])})']
    for c in issue['comments']:
        edited = f' (edited {fmt_time(c["updated"], tz)})' if c['updated'] and c['updated'] != c['created'] else ''
        out += ['', f'### {person(c["author"])} · {fmt_time(c["created"], tz)}{edited}', fence(c['body'])]
    return '\n'.join(out) + '\n'


CHANGE_KINDS = ('description', 'comments', 'links', 'subtasks', 'attachments')


def change_summary(changes: dict) -> str:
    parts = []
    for name, label, old, new in changes.get('fields', []):
        if name == 'status':
            parts.append(f'status {old or "—"} → {new or "—"}')
        elif name == 'resolution':
            parts.append(f'resolution {new or "cleared"}')
        elif name == 'priority':
            parts.append(f'priority {old or "—"} → {new or "—"}')
        elif name == 'assignee':
            parts.append(f'assignee → {new["display"] if new else "—"}')
        else:
            parts.append(label.lower())
    if 'description' in changes:
        parts.append('description')
    if comments := changes.get('comments'):
        if n := len(comments['added']):
            parts.append(f'+{n} {plural(n, "comment", "comments")}')
        if n := len(comments['edited']):
            parts.append(f'comments edited: {n}')
        if n := len(comments['deleted']):
            parts.append(f'comments deleted: {n}')
    parts += [name for name in ('links', 'subtasks', 'attachments') if name in changes]
    return ', '.join(parts)


def change_slug(key: str, changes: dict) -> str:
    kinds = [f[0] for f in changes.get('fields', [])] + [k for k in CHANGE_KINDS if k in changes]
    return f'{key.lower()}-{kinds[0].replace("_", "-")}'


def _field_value(name: str, value) -> str:
    if name == 'assignee':
        return person(value)
    if name == 'parent':
        return brief(value) if value else '—'
    if name == 'fix_versions':
        return ', '.join(value)
    return value or '—'


def _period(start: str, end: str, tz: str) -> str:
    first, last = fmt_time(start, tz), fmt_time(end, tz)
    if first == last:
        return first
    return f'{first}–{last[11:]}' if first[:10] == last[:10] else f'{first} – {last}'


def change(issue: dict, changes: dict, base_url: str, tz: str, activity: tuple, notes: dict[str, str] | None = None) -> str:

    key = issue['key']
    out = [f'# {key} · {change_summary(changes)}', '', f'{key} «{one_line(issue["summary"])}» · {browse_url(base_url, key)}']
    start, end, authors = activity
    if start:
        out.append(f'Changes {_period(start, end, tz)} ({tz})' + (f' · {", ".join(authors)}' if authors else ''))

    if fields := changes.get('fields'):
        out += ['', '## Fields', '| Field | Before | After |', '|---|---|---|']
        out += [f'| {label} | {cell(_field_value(name, old))} | {cell(_field_value(name, new))} |' for name, label, old, new in fields]

    if description := changes.get('description'):
        out += ['', '## Description']
        out.append(fence(description['text']) if description['full'] else fence(description['text'], 'diff'))

    if comments := changes.get('comments'):
        out += ['', '## Comments']
        for c in comments['added']:
            out += ['', f'### + {person(c["author"])} · {fmt_time(c["created"], tz)}', fence(c['body'])]
        for old, c in comments['edited']:
            out += ['', f'### ~ {person(c["update_author"] or c["author"])} · {fmt_time(c["updated"], tz)}'
                        f' (comment by {person(c["author"])}, {fmt_time(c["created"], tz)})',
                    fence(text_diff(old['body'], c['body']), 'diff')]
        for c in comments['deleted']:
            out += ['', f'### − deleted comment by {person(c["author"])}, {fmt_time(c["created"], tz)}']

    if links := changes.get('links'):
        out += ['', '## Links'] + [f'- + {x["phrase"]} {brief(x)}' for x in links['added']]
        out += [f'- − {x["phrase"]} {x["key"]} «{one_line(x["summary"])}»' for x in links['removed']]
    if subtasks := changes.get('subtasks'):
        out += ['', '## Subtasks'] + [f'- + {brief(x)}' for x in subtasks['added']]
        out += [f'- − {x["key"]} «{one_line(x["summary"])}»' for x in subtasks['removed']]
    if attachments := changes.get('attachments'):
        out += ['', '## Attachments']
        out += [attachment_line(a, tz, notes, prefix='+ ') for a in attachments['added']]
        out += [f'- − {a["filename"]}' for a in attachments['removed']]
    return '\n'.join(out) + '\n'


def unavailable(key: str, status: int, base_url: str) -> str:
    return (f'# {key} · issue unavailable in Jira, no longer tracked\n\n'
            f'Jira returns {status} for {browse_url(base_url, key)}: the issue was deleted, moved to another project, '
            f'or none of its watchers has access to it. Its changes are no longer published; '
            f'to watch it again: `jira watch {key} <address>`.\n')


def paused(key: str, base_url: str, watchers: list[str]) -> str:
    who = ', '.join(watchers) or 'nobody'
    return (f'# {key} · tracking paused: no working Jira token\n\n'
            f'Changes of {browse_url(base_url, key)} are not published: Jira no longer accepts the token of any '
            f'watcher ({who}). A watcher registers again with a fresh token (`jira register`), and publishing '
            f'resumes with the next poll.\n')
