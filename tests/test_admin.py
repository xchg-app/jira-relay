"""Installation settings: the config file, secrets, and the admin commands that write them."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from jira_relay import cli, config
from jira_relay.config import Agent

SAMPLE = '''
admin = "alice"
timezone = "UTC"

[jira.main]
url = "https://jira.example.com/"

[jira.other]
url = "https://jira.other.example.com"

[outbound.team]
type = "xchg"
remote = "git@git.example.com:team/hub.git"
login = "jira-bot"

[outbound.me]
type = "xchg"
remote = "git@git.example.com:alice/personal.git"
login = "alice.jira-bot"
owner = "alice"

[users.alice.agents.backend]
projects = ["main/DEMO"]
watch_to = "team:@backend"

[users.bob]
outbounds = ["team"]
'''


class ConfigTest(unittest.TestCase):
    def test_parse_round_trip_outbounds_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'config').mkdir()
            (directory / 'config' / 'jira-relay.toml').write_text(SAMPLE)
            cfg = config.load(directory)

            self.assertEqual(cfg.default_site, 'main')
            self.assertEqual(cfg.site().url, 'https://jira.example.com')
            self.assertEqual(cfg.max_attachment, 10 * 1024 * 1024)  # default
            self.assertEqual(cfg.users['alice'].agents['backend'], Agent(('main/DEMO',), 'team:@backend'))
            self.assertEqual(cfg.outbounds['team'].options, {'remote': 'git@git.example.com:team/hub.git',
                                                             'login': 'jira-bot'})
            self.assertIsNone(cfg.outbounds_of('alice'))  # the admin: all
            self.assertEqual(cfg.outbounds_of('bob'), ('team',))  # not alice's personal one
            self.assertEqual(cfg.outbounds_of('stranger'), ())

            config.save(cfg)
            again = config.load(directory)
            self.assertEqual(config.dump(again), config.dump(cfg))
            self.assertEqual(again.users['bob'].outbounds, ('team',))

            with self.assertRaises(config.ConfigError):
                cfg.jira('main')  # no service secret yet
            config.write_secret(cfg.site_secret_path('main'), 'service-pat\n')
            self.assertEqual(oct(cfg.site_secret_path('main').stat().st_mode & 0o777), '0o600')
            self.assertEqual(cfg.user_jira('alice', 'main').auth.secrets(), ['service-pat'])  # the admin: service secret
            self.assertIsNone(cfg.user_jira('bob', 'main'))
            config.write_secret(cfg.user_secret_path('bob', 'main'), 'bob-pat')
            self.assertEqual(cfg.user_jira('bob', 'main').auth.secrets(), ['bob-pat'])
            self.assertNotIn('service-pat', (directory / 'config' / 'jira-relay.toml').read_text())


class AdminCliTest(unittest.TestCase):
    """`jira-relay jira|outbound|user ...` through cli.main with JIRA_RELAY_DATA in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict('os.environ', {'JIRA_RELAY_DATA': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdin=''):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out), mock.patch('sys.stdin', io.StringIO(stdin)):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def ok(self, *argv, stdin=''):
        code, out = self.run_cli(*argv, stdin=stdin)
        self.assertEqual(code, 0, f'{argv}: {out}')
        return out

    def test_setup_flow(self):
        cfg = lambda: config.load(Path(self.tmp.name))
        self.ok('user', 'add', 'alice')
        self.assertEqual(cfg().admin, 'alice')  # the first user is the admin

        self.ok('jira', 'add', 'main', 'https://jira.example.com/')
        self.assertEqual(self.run_cli('jira', 'add', 'Bad', 'https://x')[0], 1)
        with mock.patch('jira_relay.admin.Jira.whoami', return_value='jira-bot'):
            out = self.ok('jira', 'secret', 'main', stdin='service-pat\n')
        self.assertIn('acts as jira-bot', out)
        self.assertNotIn('service-pat', out)

        self.assertEqual(self.run_cli('user', 'add', 'bob', '--outbounds', 'team')[0], 1)  # no such outbound yet
        self.ok('outbound', 'add', 'team', '--remote', 'git@git.example.com:team/hub.git', '--login', 'jira-bot')
        self.ok('outbound', 'add', 'me', '--remote', 'git@git.example.com:alice/p.git', '--login', 'b', '--owner', 'alice')
        self.assertEqual(self.run_cli('outbound', 'add', 'x', '--type', 'xchg')[0], 1)  # remote and login required
        self.ok('outbound', 'hostkey', 'team', stdin='git.example.com ssh-ed25519 AAAAC3Nza\n')
        self.assertTrue((cfg().outbound_dir('team') / 'known_hosts').exists())
        self.ok('user', 'add', 'bob', '--outbounds', 'team')
        self.assertEqual(cfg().admin, 'alice')
        self.assertEqual(cfg().outbounds_of('bob'), ('team',))

        self.ok('user', 'agent', 'bob', 'backend', '--projects', 'main/demo', '--watch-to', 'team:@backend')
        self.ok('user', 'agent', 'bob', 'ops', '--projects', 'main/*,*')
        for bad in (['--projects', 'nowhere/DEMO'], ['--projects', 'DEMO'], ['--projects', 'main/DE-MO'],
                    ['--projects', '*', '--watch-to', 'nohub:@x']):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli('user', 'agent', 'bob', 'bad', *bad)[0], 1)
        agents = cfg().users['bob'].agents
        self.assertEqual(agents, {'backend': Agent(('main/DEMO',), 'team:@backend'), 'ops': Agent(('main/*', '*'))})
        self.assertEqual(self.run_cli('user', 'rm', 'alice')[0], 1)  # the admin stays


if __name__ == '__main__':
    unittest.main()
