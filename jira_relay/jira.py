"""Jira Server/DC REST v2 client. Authentication is pluggable (Auth); today only a personal access token (PAT)."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# Allowlist: everything the snapshot and the diff need. The rest (rank, sprint, worklog) is not requested.
FIELDS = (
    'summary', 'status', 'resolution', 'issuetype', 'priority', 'assignee', 'reporter',
    'description', 'comment', 'issuelinks', 'subtasks', 'parent', 'fixVersions', 'attachment',
    'created', 'updated',
)
PAGE = 50
# networks drop now and then (e.g. DNS "[Errno -3] Try again"); only GET is retried, it is safe.
# Writes are not retried: a timeout does not mean Jira did not perform the write
RETRIES = 3
RETRY_DELAY = 3
RETRY_STATUS = frozenset({429, 502, 503, 504})


class Auth:
    """How requests are authenticated and how a user is identified. A new scheme (basic, OAuth, Cloud) is a subclass."""
    kind = ''

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def login(self, myself: dict) -> str:
        """User name from GET /rest/api/2/myself: checks that a user's token is really theirs."""
        raise NotImplementedError

    def secrets(self) -> list[str]:
        """Values that must never be echoed back (masked in replies)."""
        return []


class PatAuth(Auth):
    """Jira Server/DC personal access token: `Authorization: Bearer <token>`; users are identified by name."""
    kind = 'pat'

    def __init__(self, token: str):
        self._token = token

    def headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self._token}'}

    def login(self, myself: dict) -> str:
        return myself.get('name') or ''

    def secrets(self) -> list[str]:
        return [self._token]


AUTH_KINDS = {'pat': PatAuth}


def make_auth(kind: str, secret: str) -> Auth:
    """Auth of the given kind from its stored secret (for pat, the token itself)."""
    try:
        return AUTH_KINDS[kind](secret)
    except KeyError:
        raise ValueError(f'unknown auth "{kind}": supported {", ".join(AUTH_KINDS)}') from None


class JiraError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f'Jira {status or "unavailable"}: {message}' if status else f'Jira unavailable: {message}')
        self.status = status


def _error_text(body: bytes) -> str:
    text = body[:2000].decode('utf-8', 'replace')
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200].strip() or 'empty response'
    messages = list(data.get('errorMessages') or []) + [f'{k}: {v}' for k, v in (data.get('errors') or {}).items()]
    return '; '.join(messages) or text[:200]


class Jira:
    def __init__(self, base_url: str, auth: Auth | str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.auth = PatAuth(auth) if isinstance(auth, str) else auth
        self.timeout = timeout

    def whoami(self) -> str:
        return self.auth.login(self.get('/rest/api/2/myself'))

    def _get(self, path: str, params: dict) -> dict:
        url = f'{self.base_url}{path}?{urlencode(params)}'
        for attempt in range(1, RETRIES + 1):
            try:
                return self._fetch(url)
            except JiraError as e:
                if attempt == RETRIES or not (e.status == 0 or e.status in RETRY_STATUS):
                    raise
                time.sleep(RETRY_DELAY)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._get(path, params or {})

    def write(self, method: str, path: str, body=None, data: bytes | None = None,
              content_type: str = 'application/json'):
        """POST/PUT without retries. body is JSON, data is a ready-made body (attachments). An empty response (204) is None."""
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
        headers = {**self.auth.headers(), 'Accept': 'application/json',
                   'X-Atlassian-Token': 'no-check'}  # without it Jira rejects attachment uploads (XSRF)
        if data is not None:
            headers['Content-Type'] = content_type
        request = Request(f'{self.base_url}{path}', data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read()
        except HTTPError as e:
            raise JiraError(e.code, _error_text(e.read())) from None
        except (URLError, TimeoutError) as e:
            raise JiraError(0, str(getattr(e, 'reason', e))) from None
        return json.loads(content) if content.strip() else None

    def _fetch(self, url: str, raw: bool = False):
        request = Request(url, headers={**self.auth.headers(), 'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read() if raw else json.load(response)
        except HTTPError as e:
            raise JiraError(e.code, _error_text(e.read())) from None
        except (URLError, TimeoutError) as e:
            raise JiraError(0, str(getattr(e, 'reason', e))) from None

    def download(self, url: str) -> bytes:
        """Attachment content (`/secure/attachment/...`): the same PAT and the same retries as the API."""
        for attempt in range(1, RETRIES + 1):
            try:
                return self._fetch(url, raw=True)
            except JiraError as e:
                if attempt == RETRIES or not (e.status == 0 or e.status in RETRY_STATUS):
                    raise
                time.sleep(RETRY_DELAY)

    def issue(self, key: str) -> dict:
        raw = self._get(f'/rest/api/2/issue/{quote(key)}', {'fields': ','.join(FIELDS), 'expand': 'changelog'})
        return self._complete_comments(raw)

    def search(self, jql: str):
        """Issues by JQL with the full changelog and all comments."""
        start = 0
        while True:
            page = self._get('/rest/api/2/search', {
                'jql': jql, 'startAt': start, 'maxResults': PAGE,
                'fields': ','.join(FIELDS), 'expand': 'changelog',
            })
            issues = page.get('issues') or []
            for raw in issues:
                changelog = raw.get('changelog') or {}
                if changelog.get('total', 0) > len(changelog.get('histories') or []):
                    yield self.issue(raw['key'])  # search truncates the changelog, issue returns it in full
                else:
                    yield self._complete_comments(raw)
            start += len(issues)
            if not issues or start >= page.get('total', 0):
                return

    def _complete_comments(self, raw: dict) -> dict:
        block = (raw.get('fields') or {}).get('comment')
        if not block or block.get('total', 0) <= len(block.get('comments') or []):
            return raw
        comments = []
        while len(comments) < block['total']:
            page = self._get(f'/rest/api/2/issue/{quote(raw["key"])}/comment', {'startAt': len(comments), 'maxResults': 1000})
            if not page.get('comments'):
                break
            comments += page['comments']
        block['comments'] = comments
        return raw
