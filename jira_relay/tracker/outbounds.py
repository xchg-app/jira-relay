"""Outbounds: where changes of tracked issues go. An address is "<outbound>:<target>"; the target's grammar belongs
to the outbound's type. Today there is one type, xchg (a git hub; target `@project`, `person`, `@project:person`
or `all`). A new type (email, telegram) is a Driver registered in TYPES.
"""
from collections.abc import Sequence
from pathlib import Path

from ..config import Config, Outbound
from . import address, hub

KNOWN_HOSTS = hub.REPO_ROOT / 'docker' / 'known_hosts'


class OutboundError(Exception):
    pass


class Driver:
    def __init__(self, cfg: Config, outbound: Outbound):
        self.cfg, self.outbound = cfg, outbound

    def setup(self) -> list[str]:
        """Connect (idempotent, at service start and after `outbound add`); returns warnings."""
        return []

    def sync(self) -> str:
        """Before every poll cycle: push what is pending. Returns a warning if something is left."""
        return ''

    def post(self, target: str, slug: str, ref: str, body: str, dry_run: bool, attach: Sequence[Path]) -> str:
        raise NotImplementedError


class XchgDriver(Driver):
    """options: remote (hub git url), login (bot login in the hub). Write key: secrets/outbound/<name>/ssh_key,
    pinned host keys: secrets/outbound/<name>/known_hosts or docker/known_hosts of this repository."""
    required = ('remote', 'login')

    def setup(self) -> list[str]:
        directory = self.cfg.outbound_dir(self.outbound.name)
        key, known = directory / 'ssh_key', directory / 'known_hosts'
        if not key.exists():
            return [f'outbound {self.outbound.name}: no write key {key}']
        return hub.connect(self.outbound.name, self.outbound.options['remote'], self.outbound.options['login'],
                           self.cfg.data_dir / f'hub-{self.outbound.name}', key,
                           known if known.exists() else KNOWN_HOSTS, self.outbound.owner)

    def sync(self) -> str:
        return hub.sync(self.outbound.name)

    def post(self, target: str, slug: str, ref: str, body: str, dry_run: bool, attach: Sequence[Path]) -> str:
        return hub.post(f'{self.outbound.name}:{target}', slug, ref, body, dry_run, attach=attach)


TYPES = {'xchg': XchgDriver}


def driver(cfg: Config, name: str) -> Driver:
    try:
        outbound = cfg.outbounds[name]
    except KeyError:
        raise OutboundError(f'unknown outbound "{name}" (configured: {", ".join(cfg.outbounds) or "none"})') from None
    try:
        kind = TYPES[outbound.type]
    except KeyError:
        raise OutboundError(f'outbound {name}: unknown type "{outbound.type}" (supported: {", ".join(TYPES)})') from None
    missing = [o for o in getattr(kind, 'required', ()) if not outbound.options.get(o)]
    if missing:
        raise OutboundError(f'outbound {name}: {", ".join(missing)} required for type {outbound.type}')
    return kind(cfg, outbound)


def poster(cfg: Config):
    """post(address, slug, ref, body, dry_run=False, attach=()) for the poller and snapshots."""
    def post(full: str, slug: str, ref: str, body: str, dry_run: bool = False, attach: Sequence[Path] = ()) -> str:
        name, _, target = full.partition(':')
        return driver(cfg, name).post(target, slug, ref, body, dry_run, attach)
    return post


def parse_address(cfg: Config, value: str) -> str:
    """Normalized "<outbound>:<target>"; the target is checked by the outbound's type."""
    name = value.strip().split(':', 1)[0]
    if name in cfg.outbounds and cfg.outbounds[name].type != 'xchg':
        raise OutboundError(f'outbound {name}: type {cfg.outbounds[name].type} has no address grammar yet')
    try:
        return address.parse(value, cfg.hubs)
    except address.AddressError as e:
        raise OutboundError(str(e)) from None
