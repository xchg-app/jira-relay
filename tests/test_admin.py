"""Installation settings: the config file, secrets, and the admin commands that write them."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from jira_relay import cli, config
from jira_relay.gateway import auth

SAMPLE = '''
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
            self.assertEqual(cfg.outbounds['team'].options, {'remote': 'git@git.example.com:team/hub.git',
                                                             'login': 'jira-bot'})
            self.assertEqual(cfg.outbounds['me'].owner, 'alice')

            config.save(cfg)
            again = config.load(directory)
            self.assertEqual(config.dump(again), config.dump(cfg))

            with self.assertRaises(config.ConfigError):
                cfg.jira('main')  # no service secret yet
            config.write_secret(cfg.site_secret_path('main'), 'service-pat\n')
            self.assertEqual(oct(cfg.site_secret_path('main').stat().st_mode & 0o777), '0o600')
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
        self.ok('jira', 'add', 'main', 'https://jira.example.com/')
        self.assertEqual(self.run_cli('jira', 'add', 'Bad', 'https://x')[0], 1)
        with mock.patch('jira_relay.admin.Jira.whoami', return_value='jira-bot'):
            out = self.ok('jira', 'secret', 'main', stdin='service-pat\n')
        self.assertIn('acts as jira-bot', out)
        self.assertNotIn('service-pat', out)

        self.ok('outbound', 'add', 'team', '--remote', 'git@git.example.com:team/hub.git', '--login', 'jira-bot')
        self.ok('outbound', 'add', 'me', '--remote', 'git@git.example.com:alice/p.git', '--login', 'b', '--owner', 'alice')
        self.assertEqual(self.run_cli('outbound', 'add', 'x', '--type', 'xchg')[0], 1)  # remote and login required
        self.ok('outbound', 'hostkey', 'team', stdin='git.example.com ssh-ed25519 AAAAC3Nza\n')
        self.assertTrue((cfg().outbound_dir('team') / 'known_hosts').exists())
        self.assertEqual(self.run_cli('outbound', 'add', 'y', '--remote', 'r', '--login', 'l', '--owner', 'a b')[0], 1)

        # users register themselves; the host lists and revokes them
        self.assertIn('no users yet', self.ok('user', 'ls'))
        got = auth.register(cfg(), 'main', 'bob-pat\n', make_jira=lambda url, a: mock.Mock(whoami=lambda: 'bob'))
        self.assertIn('bob            registered', self.ok('user', 'ls'))
        self.assertNotIn(got.token, self.ok('user', 'ls'))
        self.ok('user', 'revoke', 'bob')
        self.assertFalse(cfg().user_secret_path('bob', 'main').exists())
        self.assertEqual(self.run_cli('user', 'revoke', 'bob')[0], 1)


if __name__ == '__main__':
    unittest.main()
