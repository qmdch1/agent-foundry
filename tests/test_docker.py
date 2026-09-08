import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_foundry.commands import Commands
from agent_foundry.models import Manifest
from agent_foundry.sandbox import Sandbox

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox(settings):
    if not os.environ.get("FOUNDRY_TEST_DOCKER"):
        pytest.skip("Set FOUNDRY_TEST_DOCKER=1 for isolated container tests")
    settings.docker_binary = os.environ.get("FOUNDRY_DOCKER_BINARY", "docker")
    return Sandbox(Commands(AsyncMock(), settings), settings)


async def test_real_tool_container_contract(sandbox, tmp_path):
    root = Path(__file__).parents[2] / "agent-tools/tools/csv-statistics"
    if not root.exists():
        pytest.skip("Clone the sibling agent-tools repository for seed contract tests")
    manifest = Manifest.model_validate_json((root / "manifest.json").read_text())
    image = await sandbox.build(root, manifest)
    evidence = await sandbox.validate(image, manifest)
    assert evidence["passed"] and evidence["samples"] == 2
    inspection = await sandbox.run(
        image,
        manifest,
        command=[
            "python",
            "-c",
            "import os,json; print(json.dumps({'uid':os.getuid(),'root_writable':os.access('/tool',os.W_OK)}))",
        ],
    )
    assert json.loads(inspection) == {"uid": 10001, "root_writable": False}


async def test_timeout_reaps_real_container(sandbox, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("def run(data):\n    while True: pass\n")
    manifest = Manifest(
        name="infinite-test",
        version="1.0.0",
        description="Timeout isolation test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        limits={"timeout_seconds": 1},
    )
    image = await sandbox.build(tmp_path, manifest)
    with pytest.raises(TimeoutError):
        await sandbox.run(image, manifest, {})
    remaining = await sandbox.commands.run(
        [sandbox.settings.docker_binary, "ps", "--filter", "ancestor=" + image, "--format", "{{.ID}}"]
    )
    assert not remaining.strip()


async def test_failed_tests_include_repair_diagnostics(sandbox, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app/main.py").write_text("def run(data): return {'value': 2}\n")
    (tmp_path / "tests/test_bad.py").write_text("def test_wrong():\n    assert 2 == 3\n")
    manifest = Manifest(
        name="failing-test",
        version="1.0.0",
        description="Failed assertion test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        examples=[{"prompt": "test", "input": {}, "output": {"value": 2}}],
    )
    image = await sandbox.build(tmp_path, manifest)
    with pytest.raises(Exception, match="assert 2 == 3"):
        await sandbox.validate(image, manifest)
