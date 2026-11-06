"""Agent gateway to Jira: `jira-relay serve` (http.py → service.py → commands.py). Reaches the tracker only through
tracker/api.py."""


class AgentError(Exception):
    pass
