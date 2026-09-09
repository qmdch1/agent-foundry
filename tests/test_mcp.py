import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_foundry.mcp_server import LocalTools, bounded
from agent_foundry.security import PolicyError

pytestmark = pytest.mark.integration


async def test_local_tools_execute_accounting_and_no_admin_access(container):
    tools = LocalTools(container)
    found = await tools.search("0.1 + 0.2", "installed")
    calculator = next(p for p in found["programs"] if p["name"] == "calculator")
    result = await tools.execute(calculator["program_id"], {"expression": "0.1+0.2"}, "0.1+0.2")
    assert result["result"] == {"result": "0.3"}
    assert result["usage"]["actual_llm_tokens"] == 0
    assert "host AI tokens are not measured" in result["usage_scope"]
    rows = await container.db.fetch(
        "SELECT usage_count FROM agent.programs WHERE id=%s", (calculator["program_id"],)
    )
    assert rows[0]["usage_count"] == 1
    internal = (await container.db.fetch("SELECT id FROM agent.programs WHERE name='builder-internal'"))[0]
    with pytest.raises(PolicyError):
        await tools.execute(internal["id"], {}, "internal request")


async def test_review_is_encrypted_queued_and_not_executed_inline(container):
    tools = LocalTools(container)
    container.evaluator.evaluate = AsyncMock(side_effect=AssertionError("inline evaluation"))
    container.builder.build = AsyncMock(side_effect=AssertionError("inline build"))
    result = await tools.review(
        "Synthetic repeatable grouping", "Answer delivered", "Field definitions", None
    )
    assert result["status"] == "queued"
    row = await container.queue.claim()
    assert "Answer delivered" not in row["payload"]
    assert container.queue.payload(row)["answer_context"] == "Answer delivered"
    status = await tools.job(row["id"])
    assert "payload" not in status and "error" not in status
    container.evaluator.evaluate.assert_not_awaited()
    container.builder.build.assert_not_awaited()
    with pytest.raises(PolicyError):
        await tools.install(uuid4())


async def test_real_stdio_handshake_tool_schema_execution_and_sanitized_errors(container):
    settings = container.settings
    env = {
        **os.environ,
        "FOUNDRY_DATABASE_URL": settings.database_url.get_secret_value(),
        "FOUNDRY_PROMPT_HASH_KEY": "test-hash",
        "FOUNDRY_JOB_ENCRYPTION_KEY": settings.job_encryption_key.get_secret_value(),
        "FOUNDRY_STATE_ROOT": str(settings.state_root),
        "FOUNDRY_CATALOG_ENABLED": "false",
        "FOUNDRY_LOCAL_ADMIN": "false",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_foundry.mcp_server"],
        env=env,
        cwd=str(Path(__file__).parents[1]),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Agent Foundry"
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == {
                "search_programs",
                "execute_program",
                "submit_build_review",
                "install_program",
                "job_status",
                "sync_catalog",
            }
            assert all("ctx" not in t.inputSchema.get("properties", {}) for t in listed.tools)
            found = await session.call_tool("search_programs", {"prompt": "0.1+0.2"})
            assert not found.isError
            data = found.structuredContent
            calculator = next(p for p in data["programs"] if p["name"] == "calculator")
            executed = await session.call_tool(
                "execute_program",
                {
                    "program_id": calculator["program_id"],
                    "input_data": {"expression": "0.1+0.2"},
                    "prompt": "0.1+0.2",
                },
            )
            assert not executed.isError and executed.structuredContent["result"] == {"result": "0.3"}
            queued = await session.call_tool(
                "submit_build_review", {"prompt": "Reusable grouping", "answer": "Delivered"}
            )
            assert not queued.isError and queued.structuredContent["status"] == "queued"
            status = await session.call_tool("job_status", {"job_id": queued.structuredContent["job_id"]})
            assert status.structuredContent["status"] == "PENDING"
            failed = await session.call_tool(
                "execute_program", {"program_id": str(uuid4()), "input_data": {}, "prompt": "unknown"}
            )
            assert failed.isError
            assert settings.database_url.get_secret_value() not in str(failed)


def test_output_budget_has_explicit_truncation():
    result = bounded({"payload": "x" * 5000}, 1000)
    assert result["truncated"] and len(result["preview"]) == 1000
    assert bounded({"count": 1}, 1000) == {"count": 1}


def test_generated_config_preserves_paths_and_has_no_credentials(tmp_path):
    import tomllib

    from agent_foundry.mcp_config import configuration

    root = tmp_path / "사용자 프로젝트"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services: {}")
    codex = tomllib.loads(configuration(root, "codex"))["mcp_servers"]["agent_foundry"]
    generic = json.loads(configuration(root, "json"))["mcpServers"]["agent-foundry"]
    assert codex["args"] == generic["args"]
    assert str(root) in codex["args"] and "-T" in codex["args"]
    assert "env" not in codex and "bearer_token_env_var" not in codex


async def test_local_init_env_is_private_and_does_not_overwrite(tmp_path, monkeypatch):
    from argparse import Namespace

    from agent_foundry.cli import execute

    example = (Path(__file__).parents[1] / ".env.example").read_text()
    (tmp_path / ".env.example").write_text(example)
    monkeypatch.chdir(tmp_path)
    args = Namespace(command="init-env", local=True, repository="https://github.com/example/agent-tools.git")
    await execute(args)
    text = (tmp_path / ".env").read_text()
    assert "FOUNDRY_GIT_PUSH=false" in text and "FOUNDRY_LOCAL_RELEASES_ENABLED=true" in text
    assert "FOUNDRY_LOCAL_ADMIN=true" in text and args.repository in text
    assert "CHANGE_" not in text
    with pytest.raises(PolicyError, match="already exists"):
        await execute(args)


async def test_local_commit_checkout_requires_opt_in_and_preserves_remote_identity(container, tmp_path):
    root, commands = container.settings.tool_repository_root, container.commands
    root.mkdir()
    await commands.run(["git", "init", "--initial-branch=main"], cwd=root)
    await commands.run(["git", "remote", "add", "origin", container.settings.tool_repository], cwd=root)
    tool = root / "tools" / "local-example"
    tool.mkdir(parents=True)
    (tool / "manifest.json").write_text(json.dumps({"local": True}))
    await commands.run(["git", "add", "."], cwd=root)
    await commands.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Local"],
        cwd=root,
    )
    commit = (await commands.run(["git", "rev-parse", "HEAD"], cwd=root)).decode().strip()
    container.settings.local_releases_enabled = True
    directory = await container.deployment.checkout(
        container.settings.tool_repository, commit, "tools/local-example"
    )
    assert json.loads((directory / "manifest.json").read_text()) == {"local": True}
    await commands.run(["git", "remote", "set-url", "origin", "https://example.invalid/wrong.git"], cwd=root)
    with pytest.raises(PolicyError, match="differs"):
        await container.deployment.checkout(container.settings.tool_repository, commit, "tools/local-example")
