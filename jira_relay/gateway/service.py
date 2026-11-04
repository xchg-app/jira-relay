"""Gateway entry point: an agent request from bin/jira (JSON) → reply (JSON-compatible dict).

user is the account on the service host: the client substitutes `id -un` on the host (never taken from the request JSON); empty means
the admin. The user must be in `users` of the config file. Jira uses the user's own secret per site (the service
secret for the admin), watching goes through the tracker, the journal is the gateway's own.
"""
from .. import __version__, config
from ..mask import mask
from ..tracker.api import Tracker
from . import AgentError, commands
from .journal import Journal


def serve(cfg: config.Config, user: str, request) -> dict:
    """The reply always carries the service version: the client compares it with its own and asks to update the skill."""
    return {**_serve(cfg, user, request), 'version': __version__}


def _serve(cfg: config.Config, user: str, request) -> dict:
    if not isinstance(request, dict):
        return {'ok': False, 'error': 'the request is not a JSON object'}
    try:
        login = user or cfg.admin
        if login not in cfg.users:
            raise AgentError(f'user {login} is not registered in jira-relay: ask the jira-relay admin')
        return commands.handle(request, cfg=cfg, login=login, journal=Journal(cfg.state_path), tracker=Tracker(cfg))
    except (AgentError, config.ConfigError) as e:
        return {'ok': False, 'error': mask(str(e))[0]}
