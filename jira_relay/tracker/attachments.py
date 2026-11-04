"""Issue attachments: what goes to the hub and what gets marked "get it from Jira".

Files over the limit (`max_attachment` in the config, 10 MB by default) are not downloaded at all: in the hub
they would stay forever, and git is not a file dump. A failed download still lets the note go, with a mark.
The file name is kept as in Jira, but each file goes into its own subdirectory: attachment names repeat.
"""
import re
from pathlib import Path

from ..jira import JiraError
from ..render import ATTACHED, human_size

# the xchg client itself reduces the name to one safe path component (keeps letters of any alphabet,
# collapses punctuation into "_"). All we have to do is keep the name inside its directory.
UNSAFE = re.compile(r'[\x00-\x1f/\\]+')


def safe_name(filename: str) -> str:
    """Name for the file on disk: as in Jira, without path separators and control characters."""
    stem, dot, suffix = filename.rpartition('.')
    if not dot:
        stem, suffix = filename, ''
    stem = UNSAFE.sub('_', stem).strip() or 'file'
    suffix = UNSAFE.sub('_', suffix).strip()
    return f'{stem[:100]}.{suffix[:20]}' if suffix else stem[:100]


def prepare(jira, attachments: list[dict], limit: int, directory: Path) -> tuple[list[Path], dict[str, str]]:
    """Download eligible attachments into directory. Returns the files and the marks by attachment id."""
    files, notes = [], {}
    for attachment in attachments:
        size, name = attachment['size'], attachment['filename']
        if size > limit:
            notes[attachment['id']] = f'not sent to the hub: {human_size(size)} is over the {human_size(limit)} limit, get it from Jira'
            continue
        try:
            content = jira.download(attachment['url'])
        except JiraError as e:
            notes[attachment['id']] = f'not sent to the hub: {e}, get it from Jira'
            continue
        if len(content) > limit:  # Jira lied about the size
            notes[attachment['id']] = f'not sent to the hub: {human_size(len(content))} is over the {human_size(limit)} limit, get it from Jira'
            continue
        path = directory / attachment['id'] / safe_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(path)
        notes[attachment['id']] = ATTACHED
    return files, notes
