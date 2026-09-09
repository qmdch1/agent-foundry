"""Generate portable client configuration; never modify an AI application's settings."""

import json
from pathlib import Path

from .security import PolicyError


def configuration(directory: Path, format: str):
    root = directory.resolve()
    compose = root / "docker-compose.yml"
    if not compose.is_file():
        raise PolicyError("Choose the agent-foundry checkout with docker-compose.yml")
    entry = {
        "command": "docker",
        "args": [
            "compose",
            "--project-directory",
            root.as_posix(),
            "-f",
            compose.as_posix(),
            "exec",
            "-T",
            "api",
            "foundry-mcp",
        ],
    }
    if format == "json":
        return json.dumps({"mcpServers": {"agent-foundry": entry}}, ensure_ascii=False, indent=2)
    return (
        '[mcp_servers.agent_foundry]\ncommand = "docker"\nargs = '
        + json.dumps(entry["args"], ensure_ascii=False)
        + "\nstartup_timeout_sec = 30\ntool_timeout_sec = 60"
    )
