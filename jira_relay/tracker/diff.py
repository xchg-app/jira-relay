"""Diff of two normalized issue states over the whitelist (docs/design.md).

An empty result means no significant changes and no note. Statuses and summaries of linked issues
and subtasks are not compared: those are changes of other issues, not this one.
"""
from ..issue import text
from ..render import text_diff

FIELDS = (
    ('status', 'Status'),
    ('resolution', 'Resolution'),
    ('priority', 'Priority'),
    ('summary', 'Summary'),
    ('assignee', 'Assignee'),
    ('fix_versions', 'Fix versions'),
    ('parent', 'Parent'),
)

# Changelog fields matching the whitelist: the period and authors are computed from them.
CHANGELOG_FIELDS = {
    'status', 'resolution', 'priority', 'summary', 'description', 'assignee', 'Fix Version',
    'Link', 'Attachment', 'Parent', 'IssueParentAssociation',
}


def _identity(name: str, value):
    if value is None:
        return None
    if name == 'assignee':
        return value['name']
    if name == 'parent':
        return value['key']
    return value


def _added_removed(old_items: list, new_items: list, ident) -> tuple[list, list]:
    old_by, new_by = {ident(x): x for x in old_items}, {ident(x): x for x in new_items}
    return [x for k, x in new_by.items() if k not in old_by], [x for k, x in old_by.items() if k not in new_by]


def diff(old: dict, new: dict) -> dict:
    changes = {}

    fields = [(name, label, old.get(name), new.get(name))
              for name, label in FIELDS if _identity(name, old.get(name)) != _identity(name, new.get(name))]
    if fields:
        changes['fields'] = fields

    # compare without CR: fingerprints saved before line-ending normalization contain \r\n
    old_description, new_description = text(old['description']), text(new['description'])
    if old_description != new_description:
        if not old_description.strip():
            changes['description'] = {'full': True, 'text': new_description}
        elif description_diff := text_diff(old_description, new_description):
            changes['description'] = {'full': False, 'text': description_diff}

    old_comments = {c['id']: c for c in old['comments']}
    new_ids = {c['id'] for c in new['comments']}
    added = [c for c in new['comments'] if c['id'] not in old_comments]
    edited = [(old_comments[c['id']], c) for c in new['comments']
              if c['id'] in old_comments and text(old_comments[c['id']]['body']) != text(c['body'])]
    deleted = [c for c in old['comments'] if c['id'] not in new_ids]
    if added or edited or deleted:
        changes['comments'] = {'added': added, 'edited': edited, 'deleted': deleted}

    for name, ident in (('links', lambda x: (x['phrase'], x['key'])), ('subtasks', lambda x: x['key']),
                        ('attachments', lambda x: x['id'])):
        plus, minus = _added_removed(old.get(name) or [], new.get(name) or [], ident)
        if plus or minus:
            changes[name] = {'added': plus, 'removed': minus}
    return changes


def activity(raw: dict, last_history_id: int, changes: dict) -> tuple[str | None, str | None, list[str]]:
    """Period and authors of the changes: history after last_history_id over the whitelist + comments."""
    events = []
    for history in (raw.get('changelog') or {}).get('histories') or []:
        if int(history['id']) > last_history_id and any(i.get('field') in CHANGELOG_FIELDS for i in history.get('items') or []):
            events.append((history['created'], (history.get('author') or {}).get('displayName') or ''))
    comments = changes.get('comments') or {}
    events += [(c['created'], (c['author'] or {}).get('display', '')) for c in comments.get('added', [])]
    events += [(c['updated'], (c['update_author'] or c['author'] or {}).get('display', '')) for _, c in comments.get('edited', [])]
    events = sorted(e for e in events if e[0])
    if not events:
        return None, None, []
    return events[0][0], events[-1][0], list(dict.fromkeys(author for _, author in events if author))
