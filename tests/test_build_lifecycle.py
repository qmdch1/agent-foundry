import json
import os
from unittest.mock import AsyncMock

import pytest

from agent_foundry.models import AgentRequest, Manifest

pytestmark = [pytest.mark.integration, pytest.mark.docker]


async def test_builder_git_activation_recovery_and_rollback(container, tmp_path):
    if not os.environ.get("FOUNDRY_TEST_DOCKER"):
        pytest.skip("Set FOUNDRY_TEST_DOCKER=1 for full build lifecycle")
    settings = container.settings
    settings.docker_binary = os.environ.get("FOUNDRY_DOCKER_BINARY", "docker")
    remote, root = tmp_path / "remote.git", settings.tool_repository_root
    settings.tool_repository = str(remote)
    commands = container.commands
    await commands.run(["git", "init", "--bare", "--initial-branch=main", str(remote)])
    await commands.run(["git", "clone", str(remote), str(root)])
    (root / "README.md").write_text("Isolated test repository\n")
    await commands.run(["git", "add", "README.md"], cwd=root)
    await commands.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Initialize"],
        cwd=root,
    )
    await commands.run(["git", "push", "origin", "main"], cwd=root)
    manifest = Manifest(
        name="temperature-converter",
        version="1.0.0",
        description="Convert Celsius readings to Fahrenheit deterministically",
        input_schema={
            "type": "object",
            "properties": {"celsius": {"type": "number"}},
            "required": ["celsius"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"fahrenheit": {"type": "number"}},
            "required": ["fahrenheit"],
            "additionalProperties": False,
        },
        examples=[
            {
                "prompt": "Convert 0 Celsius to Fahrenheit",
                "input": {"celsius": 0},
                "output": {"fahrenheit": 32},
            },
            {
                "prompt": "Convert 100 Celsius to Fahrenheit",
                "input": {"celsius": 100},
                "output": {"fahrenheit": 212},
            },
        ],
    )
    files = {
        "app/main.py": "def run(data):\n    return {'fahrenheit': data['celsius'] * 9 / 5 + 32}\n",
        "tests/test_main.py": "from app.main import run\ndef test_freezing():\n    assert run({'celsius': 0}) == {'fahrenheit': 32}\n",
        "README.md": "A synthetic temperature conversion test tool.",
    }
    container.builder.llm = AsyncMock()
    container.builder.llm.call.return_value = {"manifest": manifest.model_dump(mode="json"), "files": files}
    status, result = await container.builder.build(
        {"capability": "Celsius Fahrenheit temperature conversion"}
    )
    assert status == "SUCCEEDED" and result["status"] == "ACTIVE"
    original_commit = result["git_commit"]
    response = await container.service.respond(AgentRequest(prompt="Convert 0 Celsius to Fahrenheit"))
    assert response["route"] == "deterministic" and response["result"] == {"fahrenheit": 32}

    # A failing new release must leave the stable Registry pointer intact.
    tool = root / "tools/temperature-converter"
    (tool / "tests/test_main.py").write_text("def test_failure():\n    assert False\n")
    with pytest.raises(Exception):
        await container.deployment.deploy(manifest, tool, "f" * 40)
    assert (await container.registry.get(manifest.program_id))["git_commit"] == original_commit

    # Publish a second stable revision and prove rollback uses stored immutable history.
    (tool / "tests/test_main.py").write_text(files["tests/test_main.py"])
    manifest.version = "1.0.1"
    (tool / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    await commands.run(["git", "add", "--", "tools/temperature-converter"], cwd=root)
    await commands.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Upgrade test release",
        ],
        cwd=root,
    )
    await commands.run(["git", "push", "origin", "main"], cwd=root)
    second = (await commands.run(["git", "rev-parse", "HEAD"], cwd=root)).decode().strip()
    directory = await container.deployment.checkout(str(remote), second, "tools/temperature-converter")
    await container.deployment.deploy(manifest, directory, second)
    await container.deployment.rollback(manifest.program_id, original_commit)
    assert (await container.registry.get(manifest.program_id))["version"] == "1.0.0"

    # A clean server state rebuilds source, image and execution receipt from Git + Registry.
    settings.state_root = tmp_path / "new-server"
    reconciled = await container.deployment.reconcile()
    assert reconciled == [{"program_id": str(manifest.program_id), "success": True}]
    response = await container.service.respond(AgentRequest(prompt="Convert 100 Celsius to Fahrenheit"))
    assert response["result"] == {"fahrenheit": 212}
    receipt = settings.state_root / "deployments" / str(manifest.program_id) / f"{original_commit}.json"
    assert json.loads(receipt.read_text())["evidence"]["passed"]
