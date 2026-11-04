"""Tracker ↔ gateway boundary: when the services split, the gateway loses only tracker/api.py and shared code loses nothing."""
import ast
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1] / 'jira_relay'
COMMON = {'jira', 'issue', 'render', 'mask', 'config'}


def imports(path: Path) -> set[str]:
    """jira_relay modules the file imports: 'tracker.api', 'render', …"""
    package = path.relative_to(PACKAGE).parent.parts
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[:len(package) - node.level + 1])
            elif node.module and node.module.startswith('jira_relay'):
                base = []
            else:
                continue
            module = (node.module or '').removeprefix('jira_relay').lstrip('.')
            parts = base + (module.split('.') if module else [])
            if node.module:
                found.add('.'.join(parts))
            else:
                found.update('.'.join(parts + [a.name]) for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name.removeprefix('jira_relay.') for a in node.names if a.name.startswith('jira_relay.'))
    return found


def files(part: str) -> list[Path]:
    return sorted((PACKAGE / part).glob('*.py'))


class BoundaryTest(unittest.TestCase):
    def test_imports_respect_the_split(self):
        for path in files('gateway'):  # the gateway reaches the tracker only through tracker/api.py
            for name in imports(path):
                if name.startswith('tracker'):
                    self.assertEqual(name, 'tracker.api', f'{path.name} imports {name}')
        for path in files('tracker'):  # the tracker knows nothing of the gateway
            self.assertFalse({n for n in imports(path) if n.startswith('gateway')}, path.name)
        for name in COMMON:  # shared code depends only on shared code
            path = PACKAGE / f'{name}.py'
            for imported in imports(path):
                self.assertIn(imported.split('.')[0], COMMON | {''}, f'{path.name} imports {imported}')


if __name__ == '__main__':
    unittest.main()
