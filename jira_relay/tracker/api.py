"""What the agent gateway (gateway/) may ask of the tracker. The only tracker module the gateway may import.

Strings in, text out, refusal is TrackerError. The gateway says who asks (login, agent); the tracker takes the
poll cycle lock and executes.
"""
import io
from contextlib import redirect_stderr, redirect_stdout

from .. import config
from . import address, hub, ops
from .state import lock


class TrackerError(Exception):
    pass


def hub_of(target: str) -> str:
    return address.hub_of(target)


class Tracker:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg

    def _run(self, work, locked: bool = True) -> str:
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(out):
                if locked:
                    with lock(self.cfg.state_dir):
                        work()
                else:
                    work()
        except (ops.TrackerError, hub.HubError) as e:
            raise TrackerError(f'{out.getvalue()}{e}'.strip()) from None
        return out.getvalue().strip()

    def watch(self, key: str, targets: list[str], *, with_subtasks: bool, snapshot: str, login: str, agent: str) -> str:
        """snapshot: 'send' | 'none' | 'dry'. The user becomes a watcher of the issue at the addresses."""
        return self._run(lambda: ops.watch(self.cfg, key, targets, with_subtasks, snapshot, (login, agent)))

    def unwatch(self, key: str, target: str | None, *, login: str) -> str:
        """The user leaves the watchers; an address nobody watches dies, with the last address the issue."""
        return self._run(lambda: ops.unwatch(self.cfg, key, target, login))

    def subtasks(self, key: str, on: bool, *, snapshot: str) -> str:
        return self._run(lambda: ops.subtasks(self.cfg, key, on, snapshot))

    def watching(self) -> str:
        return self._run(lambda: ops.listing(self.cfg), locked=False)
