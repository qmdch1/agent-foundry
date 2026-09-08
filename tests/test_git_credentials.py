import io
import sys

import pytest

from agent_foundry.git_credentials import main


@pytest.mark.parametrize(
    "host,path,allowed",
    [
        ("github.com", "qmdch1/agent-tools.git", True),
        ("github.com", "qmdch1/another-repository.git", False),
        ("github.com", "", False),
        ("other.example", "qmdch1/agent-tools.git", False),
    ],
)
def test_git_credential_is_limited_to_the_approved_repository(monkeypatch, capsys, host, path, allowed):
    monkeypatch.setenv("FOUNDRY_TOOL_REPOSITORY", "https://github.com/qmdch1/agent-tools.git")
    monkeypatch.setenv("FOUNDRY_GIT_TOKEN", "synthetic-token")
    monkeypatch.setattr(sys, "argv", ["helper", "get"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"protocol=https\nhost={host}\npath={path}\n"))
    main()
    output = capsys.readouterr().out
    assert ("synthetic-token" in output) == allowed
