"""Publishing address: hub + project (optional) + person (optional).

xchg address grammar: `hub:@project:person`, the parts after the hub in any order. Here the hub is required:
the bot writes to several hubs, and an address without a hub is ambiguous. Hub only (`work`) means everyone
in the hub (`work:all`).
"""
import re

PROJECT_RE = re.compile(r'^@[a-z0-9._-]+$')
NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


class AddressError(ValueError):
    pass


def parse(value: str, hubs: tuple[str, ...] = ()) -> str:
    """Normalized address: `hub:@project:person`, `hub:@project`, `hub:person` or `hub:all`."""
    parts = [part.strip() for part in value.strip().split(':')]
    hub, rest = parts[0], parts[1:]
    if not NAME_RE.match(hub) or hub == 'all':
        raise AddressError(f'"{value}": an address starts with a hub, e.g. team:@backend, team:@backend:alice, me:alice')
    if hubs and hub not in hubs:
        raise AddressError(f'"{value}": hub "{hub}" is not connected to the bot (connected: {", ".join(hubs)})')
    projects = [part for part in rest if part.startswith('@')]
    people = [part for part in rest if not part.startswith('@')]
    if '' in rest or len(projects) > 1 or len(people) > 1:
        raise AddressError(f'"{value}": after the hub, at most one @project and one person')
    if projects and not PROJECT_RE.match(projects[0]):
        raise AddressError(f'"{value}": a project is @ and [a-z0-9._-], e.g. @backend')
    if people and not NAME_RE.match(people[0]):
        raise AddressError(f'"{value}": a person login is [A-Za-z0-9._-]')
    if people == ['all']:
        if projects:
            raise AddressError(f'"{value}": all means the whole hub and cannot be combined with a project')
        people = []
    tail = projects + people
    return f'{hub}:{":".join(tail)}' if tail else f'{hub}:all'


def hub_of(address: str) -> str:
    return address.split(':', 1)[0]
