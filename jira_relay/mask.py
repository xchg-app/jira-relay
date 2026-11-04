"""Masking of obvious secrets before sending: the hub is git, whatever is sent is kept forever.

Only explicit cases are caught (a keyword followed by ":"/"=", known token formats, a password in a URL).
Long random strings without a keyword are left alone: commit hashes and ids would make noise.
"""
import re

# Russian keywords stay: Jira content (issue text, comments) may be in Russian.
_KEYWORDS = (
    r'password|passwd|pwd|пароль|passphrase|token|токен|secret|секрет|'
    r'api[_-]?key|apikey|client[_-]?secret|access[_-]?key|private[_-]?key'
)


def _hidden(kind: str) -> str:
    return f'«hidden: {kind}»'


_RULES = (
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S),
     lambda m: _hidden('private key')),
    (re.compile(r'\b(?:glpat-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,})'),
     lambda m: _hidden('token')),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
     lambda m: _hidden('AWS key')),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
     lambda m: _hidden('JWT')),
    (re.compile(r'\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}', re.I),
     lambda m: f'{m.group(1)} {_hidden("token")}'),
    (re.compile(r'(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)', re.I),
     lambda m: f'{m.group(1)}{_hidden("password")}{m.group(2)}'),
    # (?!«hidden: ) — masking is idempotent: the gateway masks output that already went through mask()
    # no \b on the left: in DB_PASSWORD the keyword is preceded by "_", which is a word character
    (re.compile(rf'((?<![^\W_])(?:{_KEYWORDS})\b["\']?\s*[:=]\s*)(["\']?)(?!«hidden: )([^\s"\'<>,;]{{4,}})\2', re.I),
     lambda m: f'{m.group(1)}{_hidden("secret")}'),
)


def mask(text: str) -> tuple[str, int]:
    """Text with secrets masked and the number of replacements."""
    total = 0
    for pattern, replace in _RULES:
        text, count = pattern.subn(replace, text)
        total += count
    return text, total
