"""The two clients: Jira REST (retries) and the agent's bin/jira (local setup, version)."""
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import jira_relay
from jira_relay import jira as jira_module
from jira_relay.jira import Jira, JiraError

ROOT = Path(__file__).resolve().parents[1]


class JiraRetryTest(unittest.TestCase):
    def test_get_retries_temporary_errors_write_does_not(self):
        calls = []

        def urlopen(request, timeout):
            calls.append(request.get_method())
            if len(calls) in (1, 2, 4):  # GET: 503, 503, then ok; POST: 503
                raise HTTPError(request.full_url, 503, 'Service Unavailable', {}, io.BytesIO(b'{}'))
            return io.BytesIO(b'{"name": "jira-bot"}')

        slept = []
        client = Jira('https://jira.example.com', 'service-pat')
        with mock.patch.object(jira_module, 'urlopen', urlopen), mock.patch.object(jira_module.time, 'sleep', slept.append):
            self.assertEqual(client.whoami(), 'jira-bot')
            self.assertEqual(calls, ['GET'] * 3)
            with self.assertRaises(JiraError) as caught:
                client.write('POST', '/rest/api/2/issue/DEMO-42/comment', {'body': 'x'})
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(calls, ['GET'] * 3 + ['POST'])  # a write may have happened: never repeated
        self.assertEqual(len(slept), 2)

        def missing(request, timeout):
            calls.append(request.get_method())
            raise HTTPError(request.full_url, 404, 'Not Found', {}, io.BytesIO(b'{"errorMessages": ["no issue"]}'))

        calls.clear()
        with mock.patch.object(jira_module, 'urlopen', missing), self.assertRaisesRegex(JiraError, 'no issue'):
            client.issue('DEMO-404')
        self.assertEqual(calls, ['GET'])  # a normal error is not retried


class AgentClientTest(unittest.TestCase):
    def test_setup_pins_register_saves_token(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp}):
            os.environ.pop('JIRA_RELAY_URL', None)
            os.environ.pop('JIRA_RELAY_TOKEN', None)
            loader = importlib.machinery.SourceFileLoader('jira_client', str(ROOT / 'client' / 'bin' / 'jira'))
            client = importlib.util.module_from_spec(importlib.util.spec_from_loader('jira_client', loader))
            loader.exec_module(client)

            self.assertEqual(client.VERSION, jira_relay.__version__)  # released together
            self.assertEqual(client.CONFIG, Path(tmp) / 'jira-relay' / 'client.conf')
            self.assertEqual(client.load_settings(), {})

            def connect(url, pinned):  # the service's certificate is AA; a different pin is refused
                if pinned and pinned != 'AA':
                    raise client.ServiceError('the certificate changed')
                return mock.Mock(), 'AA'
            replies = {'/version': {'version': client.VERSION},
                       '/register': {'ok': True, 'login': 'alice', 'token': 'service-token'}}

            def call(settings, method, path, body=None, token=''):
                conn, settings['fingerprint'] = connect(settings['url'], settings.get('fingerprint', ''))
                return replies[path]
            url = 'https://relay.example.com:8443'
            with mock.patch.object(client, 'connect', connect), mock.patch.object(client, 'call', call), \
                    redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
                self.assertEqual(client.setup([url]), 0)
                self.assertEqual(client.load_settings(), {'url': url, 'fingerprint': 'AA'})
                self.assertEqual(client.setup([url, '--fingerprint', 'BB']), 1)  # the admin said BB: refused
                self.assertEqual(client.register([]), 1)  # not a terminal: the token is not read
                with mock.patch('sys.stdin.isatty', return_value=True), \
                        mock.patch.object(client.getpass, 'getpass', return_value='jira-pat'):
                    self.assertEqual(client.register(['--jira', 'main']), 0)
            self.assertEqual(client.load_settings(), {'url': url, 'fingerprint': 'AA', 'token': 'service-token'})
            self.assertEqual(oct(client.CONFIG.stat().st_mode & 0o777), '0o600')
            with mock.patch.dict(os.environ, {'JIRA_RELAY_URL': 'https://other'}):
                self.assertEqual(client.load_settings()['url'], 'https://other')  # the environment wins


if __name__ == '__main__':
    unittest.main()
