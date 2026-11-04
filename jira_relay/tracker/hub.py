"""Publishing to the hub through the xchg client (we do not write our own hub protocol implementation).

xchg runs from the root of this repository: the project is derived from the directory, and notes
are signed "<login>/jira-relay". The login comes from the xchg config; in the container it is XCHG_CONF_DIR with jira-bot.
XCHG_BIN is the path to the client if it is not in PATH.
"""
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# xchg >= 0.1.7: hub unreachable, the message is written to the local clone and goes out with the next xchg sync
PENDING = 4


class HubError(Exception):
    pass


def _xchg(*args: str, body: str | None = None, ssh: str = '') -> subprocess.CompletedProcess:
    env = {**os.environ, 'GIT_SSH_COMMAND': ssh} if ssh else None
    try:
        return subprocess.run(
            [os.environ.get('XCHG_BIN', 'xchg'), *args],
            input=body, text=True, capture_output=True, cwd=REPO_ROOT, timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError(f'xchg failed to start: {e}') from None


def _output(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout).strip()


def sync(hub: str) -> str:
    """Pull the hub and push whatever is unsent. Returns a warning if something is left."""
    result = _xchg('sync', '--hub', hub)
    if result.returncode == PENDING:
        return _output(result)
    if result.returncode != 0:
        raise HubError(f'xchg sync --hub {hub}: {_output(result)}')
    return ''


def post(address: str, slug: str, ref: str, body: str, dry_run: bool = False, attach: Sequence[Path] = ()) -> str:
    """Note to a full address (`hub:@project:person`, see address.py).

    The client puts the files (`--attach`) into the hub before the message and references them in the
    `attachments:` line; if the hub rejects a file, nothing is sent.
    """
    files = [arg for path in attach for arg in ('--attach', str(path))]
    if dry_run:
        names = ' '.join(f'--attach {Path(p).name}' for p in attach)
        print(f'----- xchg post {address} {slug} --ref {ref} {names}\n{body}')
        return ''
    result = _xchg('post', address, slug, '--ref', ref, *files, body=body)
    # PENDING is not an error: the note is already in the hub clone, sending again would duplicate it
    if result.returncode not in (0, PENDING):
        raise HubError(f'xchg post {address} {slug}: {_output(result)}')
    if result.stderr.strip():  # "not sent", "looks like a secret": to the log
        print(f'xchg post {address} {slug}: {result.stderr.strip()}', file=sys.stderr)
    return result.stdout.strip()


# --- connecting an outbound of type xchg -----------------------------------------------------------------
PROJECT = REPO_ROOT.name  # xchg signs notes "<login>/<project>"; the project is this repository
PROJECT_REPO = os.environ.get('XCHG_PROJECT_REPO', 'https://github.com/xchg-app/jira-relay')


def _registered_login(hub: str) -> str:
    """Login the hub is connected with in the bot's xchg config; empty — not connected."""
    for line in _xchg('hubs').stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip('*') == hub:
            return parts[1]
    return ''


def ssh_command(key: Path, known_hosts: Path) -> str:
    return f'ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={known_hosts}'


def connect(hub: str, remote: str, login: str, path: Path, key: Path, known_hosts: Path, owner: str) -> list[str]:
    """Connect the hub idempotently; returns warnings. Each hub has its own write key: core.sshCommand of its clone
    (GIT_SSH_COMMAND is set only for the setup calls, it would override the clone's key)."""
    warnings = []
    ssh = ssh_command(key, known_hosts)
    current = _registered_login(hub)
    if current and current != login:
        # the login changed (xchg.app assigns it by key): reconnect, the clone stays
        _xchg('hub', 'rm', hub)
        current = ''
    if not current:
        result = _xchg('hub', 'add', hub, remote, '--login', login, '--path', str(path), ssh=ssh)
        if result.returncode:
            warnings.append(f'hub {hub}: connect failed: {_output(result)}')
    else:
        origin = subprocess.run(['git', '-C', str(path), 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True).stdout.strip()
        if origin != remote:
            # the hub moved: the clone's origin and the bot config go to the new address, stuck commits follow
            result = _xchg('hub', 'remote', hub, remote, ssh=ssh)
            if result.returncode:
                warnings.append(f'hub {hub}: changing the address failed: {_output(result)}')
    if not (path / '.git').is_dir() or not _registered_login(hub):
        return warnings + [f'hub {hub} ({remote}) is not connected']
    subprocess.run(['git', '-C', str(path), 'config', 'core.sshCommand', ssh], check=False)
    # the bot's project and passport in every hub: signature <login>/<project> (a repeated call changes nothing)
    result = _xchg('projects', 'add', PROJECT, '--hub', hub, '--repo', PROJECT_REPO, '--owner', owner, ssh=ssh)
    if result.returncode:
        warnings.append(f'hub {hub}: project {PROJECT} not added: {_output(result)}')
    return warnings
