"""Agent gateway to Jira: `jira-relay agent` (entry point: service.py). Reaches the tracker only through tracker/api.py."""


class AgentError(Exception):
    pass
