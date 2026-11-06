"""Tracker: what a note looks like, what gets masked, where it goes, and the poll cycle that sends it."""
import copy
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from jira_relay import render
from jira_relay.config import Config, Outbound, Site
from jira_relay.issue import last_history_id, normalize
from jira_relay.jira import JiraError
from jira_relay.mask import mask
from jira_relay.tracker.address import AddressError, parse
from jira_relay.tracker.hub import HubError
from jira_relay.tracker.poll import Poller
from jira_relay.tracker.state import State

RAW = json.loads((Path(__file__).parent / 'fixtures' / 'issue.json').read_text())
URL = 'https://jira.example.com'


def edited(raw=RAW, **fields):
    new = copy.deepcopy(raw)
    new['fields'].update(fields)
    return new


class RenderTest(unittest.TestCase):
    def test_snapshot(self):
        text = render.snapshot(normalize(RAW), URL + '/', 'Europe/Berlin', with_subtasks=True)
        lines = text.splitlines()
        self.assertEqual(lines[0], '# DEMO-42 · snapshot: Export report | to CSV')
        for row in ('| Link | https://jira.example.com/browse/DEMO-42 |', '| Assignee | Bob Brown (bob) |',
                    '| Parent | DEMO-1 «Reports» — To Do |', '| Tracking | with subtasks |'):
            self.assertIn(row, lines)
        for heading in ('## Description', '## Subtasks', '## Links', '## Attachments', '## Comments (2)'):
            self.assertIn(f'\n{heading}\n', text)
        # Jira markup is fenced longer than the backticks inside it
        self.assertIn('````\nh3. Scope\n# first item\n{code}\n```already fenced```\n{code}\n````\n', text)
        self.assertIn('- blocks DEMO-45 «Import report» — To Do', text)
        self.assertIn('- spec.pdf — 23.4 KB, Alice Adams (alice), 2026-09-03 16:19 — '
                      'https://jira.example.com/secure/attachment/72196/spec.pdf', text)
        # comments in time order, in the configured zone, edits shown
        first = text.index('### Alice Adams (alice) · 2026-09-03 16:23\n')
        second = text.index('### Bob Brown (bob) · 2026-09-03 17:00 (edited 2026-09-03 18:30)\n')
        self.assertLess(first, second)


class MaskTest(unittest.TestCase):
    def test_secrets_hidden_prose_kept_idempotent(self):
        secrets = {
            'пароль: Qwerty123': 'Qwerty123',
            'DB_PASSWORD="s3cr3t"': 's3cr3t',
            'api_key = abcd1234': 'abcd1234',
            'Authorization: Bearer abcdefghijklmnop': 'abcdefghijklmnop',
            'postgres://app:hunter22@db.local:5432': 'hunter22',
            'ghp_' + 'A1' * 15: 'A1A1A1',
            'AKIA' + 'ABCDEFGHIJKLMNOP': 'ABCDEFGHIJKLMNOP',
            'eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q': 'SflKxwRJSMeKKF2Q',
            'before\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\nafter': 'AAAA',
        }
        for text, secret in secrets.items():
            with self.subTest(text=text):
                out, count = mask(text)
                self.assertGreater(count, 0)
                self.assertNotIn(secret, out)
                self.assertIn('«hidden:', out)
                self.assertEqual(mask(out), (out, 0))  # the gateway masks text that is already masked
        self.assertIn('@db.local', mask('postgres://app:hunter22@db.local:5432')[0])
        for prose in ('Token-based auth, the password field is required', 'commit 2d138990723fa5f42e163802569c21f77dd4d489',
                      'https://jira.example.com/browse/DEMO-42', 'Пароль — обязательное поле'):
            self.assertEqual(mask(prose), (prose, 0), prose)


class AddressTest(unittest.TestCase):
    def test_parse(self):
        hubs = ('team', 'me')
        for value, expected in {'team:@backend': 'team:@backend', 'team:bob:@backend': 'team:@backend:bob',
                                'me:alice': 'me:alice', ' team ': 'team:all', 'team:all': 'team:all'}.items():
            self.assertEqual(parse(value, hubs), expected, value)
        for value in ('@backend', 'all', 'team:', 'team:@a:@b', 'team:alice:bob', 'team:@Backend',
                      'team:@backend:all', '', 'other:@backend'):
            with self.subTest(value=value), self.assertRaises(AddressError):
                parse(value, hubs)


class FakeJira:
    """Answers `key in (...)` / `parent in (...)` from the issues it holds."""

    def __init__(self, *raws):
        self.issues = {r['key']: r for r in raws}

    def search(self, query):
        field, keys = re.match(r'(key|parent) in \(([^)]*)\)', query).groups()
        keys = {k.strip() for k in keys.split(',')}
        for raw in self.issues.values():
            parent = (raw['fields'].get('parent') or {}).get('key')
            if (raw['key'] if field == 'key' else parent) in keys:
                yield raw

    def issue(self, key):
        if key not in self.issues:
            raise JiraError(404, 'Issue Does Not Exist')
        return self.issues[key]

    def download(self, url):
        return b'content'


class PollTest(unittest.TestCase):
    """A real sqlite state; Jira and the outbound are fakes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(Path(self.tmp.name), tz='UTC', sites={'main': Site('main', URL)},
                          outbounds={n: Outbound(n, 'xchg') for n in ('team', 'me')})
        self.state = State(self.cfg.state_path)
        self.addCleanup(self.state.db.close)
        self.posts, self.failing = [], set()

    def track(self, key, raw, addresses, with_subtasks=False):
        ref = f'main/{key}'
        self.state.track(ref, with_subtasks, None)
        for address in addresses:
            self.state.add_address(ref, address)
            self.state.save_delivery(ref, address, normalize(raw), last_history_id(raw))

    def post(self, address, slug, ref, body, dry_run, attach=()):
        if address in self.failing:
            raise HubError('push rejected')
        self.posts.append((address, slug, ref, body))
        return ''

    def poll(self, jira):
        with redirect_stdout(io.StringIO()):
            return Poller(self.cfg, self.state, jira, self.post).run()

    def sent(self):
        return [(address, slug) for address, slug, _, _ in self.posts]

    def test_change_posts_one_masked_note_once(self):
        self.track('DEMO-42', RAW, ['team:@backend'])
        self.assertEqual(self.poll(FakeJira(RAW)), 0)
        self.assertEqual(self.posts, [])  # unchanged: nothing

        comments = copy.deepcopy(RAW['fields']['comment'])
        comments['comments'].append({'id': '21', 'author': {'name': 'bob', 'displayName': 'Bob Brown'},
                                     'created': '2026-09-11T10:12:00.000+0000', 'updated': '2026-09-11T10:12:00.000+0000',
                                     'body': 'Deployed, token = abcd1234efgh'})
        jira = FakeJira(edited(status={'name': 'Review'}, comment=comments))
        self.assertEqual(self.poll(jira), 0)
        self.assertEqual(self.poll(jira), 0)
        (address, slug, ref, body), = self.posts
        self.assertEqual((address, slug, ref), ('team:@backend', 'demo-42-status', f'{URL}/browse/DEMO-42'))
        self.assertTrue(body.startswith('# DEMO-42 · status In Progress → Review, +1 comment\n'), body)
        self.assertIn('| Status | In Progress | Review |', body)
        self.assertNotIn('abcd1234efgh', body)
        self.assertIsNotNone(self.state.get_meta('last_poll:main'))

    def test_failing_address_is_retried_without_duplicates(self):
        self.track('DEMO-42', RAW, ['team:@backend', 'me:alice'])
        jira = FakeJira(edited(status={'name': 'Review'}))
        self.failing = {'me:alice'}
        self.assertEqual(self.poll(jira), 1)
        self.assertIsNone(self.state.get_meta('last_poll:main'))  # the window does not move
        self.assertEqual(self.sent(), [('team:@backend', 'demo-42-status')])
        self.failing = set()
        self.assertEqual(self.poll(jira), 0)
        self.assertEqual(self.sent(), [('team:@backend', 'demo-42-status'), ('me:alice', 'demo-42-status')])
        self.assertIsNotNone(self.state.get_meta('last_poll:main'))

    def test_new_subtask_goes_to_parent_addresses(self):
        self.track('DEMO-42', RAW, ['team:@backend', 'me:alice'], with_subtasks=True)
        subtask = {'key': 'DEMO-47', 'fields': {'summary': 'Third step', 'status': {'name': 'To Do'},
                                                'parent': {'key': 'DEMO-42', 'fields': {'summary': 'Export'}}}}
        self.poll(FakeJira(RAW, subtask))
        self.assertEqual(self.sent(), [('team:@backend', 'demo-47-snapshot'), ('me:alice', 'demo-47-snapshot')])
        self.assertEqual(self.state.tracked('main/DEMO-47')['parent_key'], 'main/DEMO-42')
        self.poll(FakeJira(RAW, subtask))
        self.assertEqual(len(self.posts), 2)
        self.poll(FakeJira(RAW, edited(subtask, status={'name': 'Done'})))
        self.assertEqual(self.sent()[2:], [('team:@backend', 'demo-47-status'), ('me:alice', 'demo-47-status')])


class MigrationTest(unittest.TestCase):
    def test_added_by_becomes_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            db = sqlite3.connect(path)
            db.executescript("""
                create table addresses (key text, address text, added_by text not null default '', primary key (key, address));
                insert into addresses values ('main/DEMO-1', 'team:@a', 'bob/backend'), ('main/DEMO-1', 'me:alice', 'backend'),
                                             ('main/DEMO-2', 'team:@b', '');
            """)
            db.commit(), db.close()
            state = State(path)
            self.assertEqual(state.owners('main/DEMO-1'), {'team:@a': ['bob/backend'], 'me:alice': []})
            self.assertEqual(state.ownerless(), [('main/DEMO-1', 'me:alice'), ('main/DEMO-2', 'team:@b')])
            self.assertEqual([r[1] for r in state.db.execute('pragma table_info(addresses)')], ['key', 'address'])
            state.db.close()


if __name__ == '__main__':
    unittest.main()
