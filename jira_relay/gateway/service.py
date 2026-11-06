"""Gateway entry point: an agent request from bin/jira (JSON, delivered by http.py) → reply (JSON-compatible dict).

login is who the request is from: a registered user (gateway/auth.py), never taken from the request JSON. Jira uses
the user's own secret per site, watching goes through the tracker, the journal is the gateway's own.
"""
from .. import __version__, config
from ..mask import mask
from ..tracker.api import Tracker
from . import AgentError, commands
from .auth import Users
from .journal import Journal


def serve(cfg: config.Config, login: str, request) -> dict:
    """The reply always carries the service version: the client compares it with its own and asks to update the skill."""
    return {**_serve(cfg, login, request), 'version': __version__}


def _serve(cfg: config.Config, login: str, request) -> dict:
    if not isinstance(request, dict):
        return {'ok': False, 'error': 'the request is not a JSON object'}
    try:
        if not login or not Users(cfg.state_path).exists(login):
            raise AgentError(f'user {login or "?"} is not registered in jira-relay: register with your Jira token '
                             f'(jira register)')
        return commands.handle(request, cfg=cfg, login=login, journal=Journal(cfg.state_path), tracker=Tracker(cfg))
    except (AgentError, config.ConfigError) as e:
        return {'ok': False, 'error': mask(str(e))[0]}
