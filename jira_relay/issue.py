"""Raw Jira issue JSON -> normalized state over a whitelist of fields.

The normalized state is both the material for a snapshot and the fingerprint for the diff: anything
not in it (rank, sprint, worklog, watchers) does not count as a change.
"""


def text(value: str | None) -> str:
    """Jira text without CR: descriptions and comments there may have \\r\\n, and the hub is git with LF normalization."""
    return (value or '').replace('\r\n', '\n').replace('\r', '\n')


def _name(obj) -> str | None:
    return (obj or {}).get('name') or None


def _user(user) -> dict | None:
    if not user:
        return None
    return {'name': user.get('name') or user.get('key') or '', 'display': user.get('displayName') or user.get('name') or ''}


def _brief(other: dict) -> dict:
    fields = other.get('fields') or {}
    return {'key': other['key'], 'summary': fields.get('summary') or '', 'status': _name(fields.get('status'))}


def _links(items) -> list[dict]:
    links = []
    for link in items or []:
        kind = link.get('type') or {}
        if 'outwardIssue' in link:
            phrase, other = kind.get('outward') or kind.get('name'), link['outwardIssue']
        elif 'inwardIssue' in link:
            phrase, other = kind.get('inward') or kind.get('name'), link['inwardIssue']
        else:
            continue
        links.append({'id': link['id'], 'phrase': phrase, **_brief(other)})
    return sorted(links, key=lambda x: (x['phrase'], x['key']))


def normalize(raw: dict) -> dict:
    f = raw.get('fields') or {}
    comments = [
        {'id': c['id'], 'author': _user(c.get('author')), 'update_author': _user(c.get('updateAuthor')),
         'created': c.get('created'), 'updated': c.get('updated'), 'body': text(c.get('body'))}
        for c in (f.get('comment') or {}).get('comments') or []
    ]
    attachments = [
        {'id': a['id'], 'filename': a.get('filename') or '', 'size': a.get('size') or 0,
         'author': _user(a.get('author')), 'created': a.get('created'), 'url': a.get('content') or ''}
        for a in f.get('attachment') or []
    ]
    parent = f.get('parent')
    return {
        'key': raw['key'],
        'summary': f.get('summary') or '',
        'type': _name(f.get('issuetype')),
        'status': _name(f.get('status')),
        'resolution': _name(f.get('resolution')),
        'priority': _name(f.get('priority')),
        'assignee': _user(f.get('assignee')),
        'reporter': _user(f.get('reporter')),
        'description': text(f.get('description')),
        'parent': _brief(parent) if parent else None,
        'fix_versions': sorted(v.get('name') or '' for v in f.get('fixVersions') or []),
        'comments': sorted(comments, key=lambda c: (c['created'] or '', int(c['id']))),
        'links': _links(f.get('issuelinks')),
        'subtasks': sorted((_brief(s) for s in f.get('subtasks') or []), key=lambda s: s['key']),
        'attachments': sorted(attachments, key=lambda a: (a['created'] or '', int(a['id']))),
        'created': f.get('created'),
        'updated': f.get('updated'),
    }


def last_history_id(raw: dict) -> int:
    return max((int(h['id']) for h in (raw.get('changelog') or {}).get('histories') or []), default=0)
