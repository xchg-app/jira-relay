"""Agent gateway: an agent request (as bin/jira sends it) → reply, Jira calls, journal, watching."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jira_relay.config import Config, Outbound, Site
from jira_relay.gateway import auth, commands, service
from jira_relay.gateway.journal import Journal
from jira_relay.jira import JiraError, PatAuth
from jira_relay.tracker import hub
from jira_relay.tracker.api import Tracker
from jira_relay.tracker.state import State

RAW = json.loads((Path(__file__).parent / 'fixtures' / 'issue.json').read_text())
SINCE = '2000-01-01T00:00:00Z'


class FakeJira:
    """Records requests; GET replies by path, write replies by (method, path)."""

    def __init__(self, gets=None, writes=None, token='service-pat'):
        self.gets, self.writes, self.calls = gets or {}, writes or {}, []
        self.auth = PatAuth(token)

    def whoami(self):
        return self.auth.login(self.get('/rest/api/2/myself'))

    def issue(self, key):
        self.calls.append(('GET', 'issue', key))
        return RAW

    def search(self, jql):
        return iter(())

    def download(self, url):
        return b'pdf'

    def get(self, path, params=None):
        self.calls.append(('GET', path, params))
        reply = self.gets.get(path, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def write(self, method, path, body=None, data=None, content_type='application/json'):
        self.calls.append((method, path, body))
        return self.writes.get((method, path))

    def changes(self):
        return [c for c in self.calls if c[0] != 'GET']


def make_config(directory: Path) -> Config:
    """Sites main (default) and other; outbounds team and me (alice's)."""
    xchg = {'remote': 'git@git.example.com:team/hub.git', 'login': 'jira-bot'}
    return Config(
        directory, tz='UTC', max_attachment=1000,
        sites={'main': Site('main', 'https://jira.example.com'), 'other': Site('other', 'https://jira.other.example.com')},
        outbounds={'team': Outbound('team', 'xchg', xchg), 'me': Outbound('me', 'xchg', xchg, owner='alice')})


class GatewayCase(unittest.TestCase):
    """A real Config in a temp data dir; alice (tokens for both sites) and bob (none yet) are registered.
    Config.user_jira is patched: self.jiras[site] when the user's secret file for the site exists, as the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name))
        self.journal = Journal(self.cfg.state_path)
        self.addCleanup(self.journal.db.close)
        self.users = auth.Users(self.cfg.state_path)
        self.addCleanup(self.users.db.close)
        for login in ('alice', 'bob'):
            self.users.put(login, f'{login}-service-token')
        self.give_token('alice', 'main')
        self.give_token('alice', 'other')
        self.jiras = {'main': FakeJira(), 'other': FakeJira()}

        def user_jira(cfg, login, site=None):
            site = cfg.site(site).name
            return self.jiras[site] if cfg.user_secret_path(login, site).exists() else None
        self.patch(Config, 'user_jira', user_jira)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def give_token(self, login, site):
        path = self.cfg.user_secret_path(login, site)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{login}-pat\n')

    def make_jira(self, url, auth):
        """Checks a new token: <login>-pat belongs to <login>."""
        (token,) = auth.secrets()
        if token.startswith('bad'):
            return FakeJira(gets={'/rest/api/2/myself': JiraError(401, f'token {token} is not valid')})
        return FakeJira(gets={'/rest/api/2/myself': {'name': token.removesuffix('-pat')}}, token=token)

    def ask(self, argv, user='alice', who='backend', text=''):
        request = {'as': who, 'argv': argv, 'text': text}
        return commands.handle(request, cfg=self.cfg, login=user, journal=self.journal, tracker=Tracker(self.cfg),
                               make_jira=self.make_jira)

    def rows(self):
        return [(r['agent'], r['op'], r['target'], r['summary']) for r in self.journal.since(SINCE)]


class WriteTest(GatewayCase):
    def test_write_permission_signature_journal(self):
        main = self.jiras['main']
        main.writes[('POST', '/rest/api/2/issue/DEMO-42/comment')] = {'id': '501'}
        reply = self.ask(['comment', 'demo-42'], text='Fixed, please check.\n')
        self.assertEqual(reply, {'ok': True, 'out': 'https://jira.example.com/browse/DEMO-42'
                                                    '?focusedCommentId=501#comment-501'})
        self.assertEqual(main.changes(), [('POST', '/rest/api/2/issue/DEMO-42/comment',
                                           {'body': 'Fixed, please check.\n\n_— agent backend_'})])
        self.assertEqual(self.rows(), [('alice/backend', 'comment', 'main/DEMO-42', 'Fixed, please check.')])

        # a user without a token for the site never reaches Jira; an unregistered login neither
        self.assertIn('bob has no Jira token for main', self.ask(['comment', 'DEMO-42'], user='bob', text='x')['error'])
        self.give_token('bob', 'main')
        main.writes[('PUT', '/rest/api/2/issue/DEMO-42/assignee')] = None
        self.assertTrue(self.ask(['assign', 'DEMO-42', 'bob'], user='bob')['ok'])
        self.assertEqual(self.rows()[-1], ('bob/backend', 'assign', 'main/DEMO-42', 'assignee → bob'))
        reply = service.serve(self.cfg, 'mallory', {'as': 'backend', 'argv': ['get', 'DEMO-42']})
        self.assertFalse(reply['ok'])
        self.assertIn('user mallory is not registered', reply['error'])
        self.assertIn('version', reply)

    def test_api_allowlist(self):
        main = self.jiras['main']
        refused = (
            (['api', 'DELETE', '/rest/api/2/issue/DEMO-42'], '', 'DELETE is not allowed'),
            (['api', 'POST', '/rest/api/2/user'], '{"name": "eve"}', 'only the jira-relay admin changes that'),
            (['api', 'PUT', '/rest/api/2/project/DEMO'], '{}', 'only the jira-relay admin changes that'),
            (['api', 'GET', '/rest/auth/1/session'], '', 'only /rest/api/2/'),
        )
        for argv, text, message in refused:
            with self.subTest(argv=argv):
                reply = self.ask(argv, text=text)
                self.assertFalse(reply['ok'])
                self.assertIn(message, reply['error'])
        self.assertEqual(main.changes(), [])

        main.gets['/rest/api/2/issue/DEMO-42/transitions'] = {'transitions': []}
        self.assertTrue(self.ask(['api', 'GET', '/rest/api/2/issue/DEMO-42/transitions'])['ok'])
        main.writes[('POST', '/rest/api/2/issue/DEMO-42/comment')] = {'id': '7'}
        self.assertTrue(self.ask(['api', 'POST', '/rest/api/2/issue/DEMO-42/comment'], text='{"body": "Done"}')['ok'])
        self.assertEqual(main.changes(), [('POST', '/rest/api/2/issue/DEMO-42/comment',
                                           {'body': 'Done\n\n_— agent backend_'})])
        self.assertEqual([r[:3] for r in self.rows()], [('alice/backend', 'api', 'main/DEMO-42')])


class TokenTest(GatewayCase):
    def test_token_per_site_checked_saved_never_echoed(self):
        reply = self.ask(['get', 'DEMO-42'], user='bob')
        self.assertIn('bob has no Jira token for main; put yours — jira token set --jira main', reply['error'])

        reply = self.ask(['token', 'set', '--jira', 'other'], user='bob', text='alice-pat\n')  # someone else's
        self.assertIn('belongs to other user "alice"', reply['error'])
        self.assertNotIn('alice-pat', reply['error'])
        reply = self.ask(['token', 'set', '--jira', 'other'], user='bob', text='bad-secret-value')
        self.assertFalse(reply['ok'])
        self.assertNotIn('bad-secret-value', reply['error'])
        self.assertFalse(self.cfg.user_secret_path('bob', 'other').exists())

        reply = self.ask(['token', 'set', '--jira', 'other'], user='bob', text='bob-pat\n')
        self.assertEqual(reply, {'ok': True, 'out': 'token for other saved; Jira actions there come from bob'})
        self.assertEqual(self.cfg.user_secret_path('bob', 'other').read_text(), 'bob-pat\n')
        self.assertEqual(oct(self.cfg.user_secret_path('bob', 'other').stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.rows(), [])  # the token is not journaled

        self.assertTrue(self.ask(['get', 'other/DEMO-42'], user='bob')['ok'])
        self.assertIn('no Jira token for main', self.ask(['get', 'DEMO-42'], user='bob')['error'])
        self.assertIn('is set', self.ask(['--jira', 'other', 'token', 'status'], user='bob')['out'])


class RegisterTest(GatewayCase):
    def test_register_issues_service_token_revoke_forgets(self):
        with self.assertRaises(commands.AgentError):
            auth.register(self.cfg, 'main', 'bad-pat', make_jira=self.make_jira)
        self.assertFalse(self.cfg.user_secret_path('bad', 'main').exists())

        got = auth.register(self.cfg, 'main', 'carol-pat\n', make_jira=self.make_jira)
        self.assertEqual(got.login, 'carol')  # the login is what Jira says, nothing else
        self.assertEqual(self.cfg.user_secret_path('carol', 'main').read_text(), 'carol-pat\n')
        self.assertEqual(auth.authenticate(self.cfg, got.token), 'carol')
        self.assertNotIn(got.token, [r['token_hash'] for r in self.users.all()])
        self.assertTrue(self.ask(['get', 'DEMO-42'], user='carol')['ok'])

        again = auth.register(self.cfg, 'main', 'carol-pat', make_jira=self.make_jira)  # renewal: the old token dies
        self.assertNotEqual(again.token, got.token)
        with self.assertRaises(commands.AgentError):
            auth.authenticate(self.cfg, got.token)
        self.assertTrue(self.users.revoke('carol'))
        with self.assertRaises(commands.AgentError):
            auth.authenticate(self.cfg, again.token)


class WatchTest(GatewayCase):
    """watch goes through the tracker: real sqlite state, the snapshot is posted through a patched hub.post."""

    def setUp(self):
        super().setUp()
        self.state = State(self.cfg.state_path)
        self.addCleanup(self.state.db.close)
        self.posts = []

        def post(address, slug, ref, body, dry_run=False, attach=()):
            self.posts.append((address, slug))
            return ''
        self.patch(Config, 'jira', lambda cfg, site=None: FakeJira())
        self.patch(hub, 'post', post)

    def test_watch_outbounds_and_sites(self):
        self.assertIn('where to send updates?', self.ask(['watch', 'DEMO-42'])['error'])  # the address is explicit
        reply = self.ask(['watch', 'DEMO-42', 'team:@backend'])
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(self.posts, [('team:@backend', 'demo-42-snapshot')])
        self.assertEqual(self.state.address_owners('main/DEMO-42'), {'team:@backend': 'alice/backend'})
        self.assertEqual(self.rows(), [('alice/backend', 'watch', 'main/DEMO-42', 'team:@backend')])

        self.assertTrue(self.ask(['watch', 'other/DEMO-42', 'me:alice'])['ok'])  # same key, another site
        self.assertEqual(self.state.addresses('main/DEMO-42'), ['team:@backend'])
        self.assertEqual(self.state.addresses('other/DEMO-42'), ['me:alice'])
        self.assertIn('connected: team, me', self.ask(['watch', 'DEMO-42', 'nohub:@x'])['error'])

        # bob: needs his own token for the site; any outbound
        self.assertIn('no Jira token for main', self.ask(['watch', 'DEMO-42', 'team:@backend:bob'], user='bob')['error'])
        self.give_token('bob', 'main')
        self.assertTrue(self.ask(['watch', 'DEMO-42', 'team:@backend:bob'], user='bob')['ok'])
        self.assertEqual(self.state.address_owners('main/DEMO-42'),
                         {'team:@backend': 'alice/backend', 'team:@backend:bob': 'bob/backend'})
        self.assertIn('me:alice', self.ask(['watching'], user='bob')['out'])

        self.assertTrue(self.ask(['unwatch', 'DEMO-42', '--jira', 'other'])['ok'])
        self.assertIsNone(self.state.tracked('other/DEMO-42'))
        self.assertIsNotNone(self.state.tracked('main/DEMO-42'))
        self.assertEqual(len(self.posts), 3)


class SitesTest(GatewayCase):
    def test_sites(self):
        self.give_token('bob', 'other')
        out = self.ask(['sites'], user='bob', who='scratch')['out']
        self.assertIn('* main         https://jira.example.com  (no token yet, set it with jira token set --jira main)', out)
        self.assertIn('other        https://jira.other.example.com  (your token set)', out)
        self.assertIn('you: bob; agent scratch', out)
        self.assertIn('watch to: team:…, me:…', out)


if __name__ == '__main__':
    unittest.main()
