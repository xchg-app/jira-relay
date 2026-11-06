"""The gateway as an HTTPS service: `jira-relay serve`. One port, the agent client talks JSON to it.

    POST /register   {"site": "main", "token": "<Jira PAT>"} → {"ok": true, "login": "alice", "token": "<service token>"}
    POST /agent      Authorization: Bearer <service token>; body: the agent request (commands.py) → its reply
    GET  /version    {"version": "0.6.0"}

Every reply is JSON and carries the service version. The login comes from the service token alone
(gateway/auth.py). TLS is a self-signed certificate the entrypoint generates in /data/secrets/tls; the client pins its
SHA-256 fingerprint at `jira setup` (`jira-relay tls` prints it for the admin to announce). Requests are served one
at a time: commands redirect stdout while the tracker prints, which is not thread-safe; the threads only keep a
slow or stuck client from blocking the port. The config is read on every request: admin commands change it while
the service runs.
"""
import hashlib
import json
import ssl
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__, config
from ..jira import Jira
from ..mask import mask
from . import AgentError, auth, service

MAKE_JIRA = Jira  # (url, auth) → Jira: checks the token at registration; tests replace it
READ_TIMEOUT = 60  # seconds to receive a request
BODY_MARGIN = 1024 * 1024  # on top of the attachments' base64 (max_attachment per file, two files is typical)


def log(message: str) -> None:
    print(f'{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}', flush=True)


def fingerprint(cert_path: Path) -> str:
    """SHA-256 of the DER certificate, colon-separated, as `openssl x509 -fingerprint -sha256` prints it."""
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2))


class Handler(BaseHTTPRequestHandler):
    server_version = f'jira-relay/{__version__}'
    sys_version = ''
    timeout = READ_TIMEOUT
    protocol_version = 'HTTP/1.1'
    lock = threading.Lock()  # one request at a time (module docstring)

    def log_message(self, format, *args):  # noqa: A002 — the base class signature
        pass  # the request line is logged by _reply, with the login and without query strings

    def _json(self, status: int, reply: dict, login: str = '') -> None:
        body = json.dumps({**reply, 'version': __version__}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        log(f'{self.address_string()} {self.command} {self.path} {login or "-"} {status}')

    def _error(self, status: int, message: str, login: str = '') -> None:
        self._json(status, {'ok': False, 'error': mask(message)[0]}, login)

    def _body(self, cfg: config.Config) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if length > 4 * cfg.max_attachment + BODY_MARGIN:
            raise AgentError(f'request too large ({length} bytes); attachments go up to {cfg.max_attachment} bytes each')
        try:
            data = json.loads(self.rfile.read(length) or b'{}')
        except ValueError as e:
            raise AgentError(f'request is not JSON: {e}') from None
        if not isinstance(data, dict):
            raise AgentError('the request is not a JSON object')
        return data

    def do_GET(self):
        if self.path == '/version':
            return self._json(200, {'ok': True})
        self._error(404, 'not found')

    def do_POST(self):
        try:
            cfg = config.load()
        except config.ConfigError as e:
            return self._error(500, str(e))
        try:
            if self.path == '/register':
                return self._register(cfg)
            if self.path == '/agent':
                return self._agent(cfg)
            self._error(404, 'not found')
        except AgentError as e:
            self._error(400, str(e))
        except config.ConfigError as e:
            self._error(500, str(e))

    def _register(self, cfg: config.Config) -> None:
        data = self._body(cfg)
        token = str(data.get('token') or '')
        if not token:
            raise AgentError('token: the Jira personal access token is required')
        site = str(data.get('site') or '')
        with self.lock:
            got = auth.register(cfg, site or None, token, make_jira=MAKE_JIRA)
        self._json(200, {'ok': True, 'login': got.login, 'token': got.token}, got.login)

    def _agent(self, cfg: config.Config) -> None:
        scheme, _, token = (self.headers.get('Authorization') or '').partition(' ')
        if scheme.casefold() != 'bearer' or not token.strip():
            return self._error(401, 'no service token: register first (jira register)')
        try:
            login = auth.authenticate(cfg, token)
        except AgentError as e:
            return self._error(401, str(e))
        data = self._body(cfg)
        with self.lock:
            reply = service.serve(cfg, login, data)
        self._json(200, reply, login)


def make_server(bind: str, port: int, context: ssl.SSLContext | None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    if context:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def serve(cfg: config.Config, bind: str, port: int) -> int:
    """Until SIGTERM. The certificate is /data/secrets/tls/{cert,key}.pem (docker/entrypoint.sh makes it)."""
    cert, key = cfg.tls_dir / 'cert.pem', cfg.tls_dir / 'key.pem'
    if not (cert.exists() and key.exists()):
        raise config.ConfigError(f'no TLS certificate in {cfg.tls_dir}: the container entrypoint generates one '
                                 f'(openssl req -x509 … -keyout key.pem -out cert.pem)')
    server = make_server(bind, port, tls_context(cert, key))
    log(f'jira-relay {__version__} gateway on https://{bind}:{port}, certificate SHA-256 {fingerprint(cert)}')
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
