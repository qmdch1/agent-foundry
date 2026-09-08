from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_foundry.builder import Builder, benefit
from agent_foundry.commands import Commands
from agent_foundry.executor import resolve_input
from agent_foundry.models import Bundle, Evaluation, Manifest
from agent_foundry.primitives import builtin, calculate
from agent_foundry.security import PolicyError, decrypt_payload, encrypt_payload, mask, safe_path
from agent_foundry.seed import seed_manifests


def test_decimal_calculation():
    assert calculate("0.1 + 0.2") == {"result": "0.3"}
    assert calculate("(15 - 3) / 4") == {"result": "3"}


@pytest.mark.parametrize(
    "expression", ["__import__('os')", "2 ** 999999", "[1] * 900000", "True + 1", "1 / 0"]
)
def test_calculator_rejects_unsafe_or_invalid(expression):
    with pytest.raises(Exception):
        calculate(expression)


def test_schema_cannot_fetch_external_ref():
    data = seed_manifests()[0].model_dump()
    data["input_schema"] = {"type": "object", "$ref": "http://127.0.0.1/secret"}
    with pytest.raises(ValueError):
        Manifest.model_validate(data)


def test_path_escape_and_symlink(tmp_path):
    for path in ("../secret", "/etc/passwd", "C:\\secret", "a/../../secret"):
        with pytest.raises(PolicyError):
            safe_path(tmp_path, path)
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(PolicyError):
        safe_path(tmp_path, "link/secret")


def test_encrypted_queue_and_secret_masking(settings):
    value = encrypt_payload({"prompt": "private information"}, settings.job_encryption_key.get_secret_value())
    assert "private" not in value
    assert (
        decrypt_payload(value, settings.job_encryption_key.get_secret_value())["prompt"]
        == "private information"
    )
    assert "hunter2" not in mask("password=hunter2 token=abcdef1234")


def test_cost_gate_prevents_expensive_trivial_tool(settings):
    e = Evaluation(
        **{k: 1 for k in settings.evaluation_weights},
        maintenance_cost=0,
        estimated_saved_tokens_per_use=50,
        capability="simple operation",
        reason="small benefit",
    )
    assert not benefit(e, settings)["approved"]
    e.estimated_saved_tokens_per_use = 10000
    assert benefit(e, settings)["approved"]


async def test_file_writer_does_not_overwrite(settings):
    settings.file_root.mkdir()
    await builtin("file-writer", {"path": "one.txt", "content": "original"}, settings)
    with pytest.raises(FileExistsError):
        await builtin("file-writer", {"path": "one.txt", "content": "replacement"}, settings)
    assert (settings.file_root / "one.txt").read_text() == "original"


def test_chained_results():
    assert resolve_input({"values": {"$from_step": 1, "path": ["data", 0]}}, {1: {"data": [[1, 2]]}}) == {
        "values": [1, 2]
    }


def test_no_key_constraints_in_schema():
    path = Path(__file__).parents[1] / "src/agent_foundry/schema.sql"
    text = path.read_text().upper()
    assert all(token not in text for token in ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE"))


async def test_command_output_and_timeout_limits(settings):
    import sys

    commands = Commands(AsyncMock(), settings)
    with pytest.raises(Exception):
        await commands.run([sys.executable, "-c", "print('x'*10000)"], limit=1024)
    with pytest.raises(TimeoutError):
        await commands.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)


def test_builder_rejects_control_plane_files(settings, tmp_path):
    m = Manifest(
        name="safe-demo",
        version="1.0.0",
        description="Safe demonstration tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    builder = Builder(None, None, None, None, None, settings)
    bundle = Bundle(
        manifest=m,
        files={"app/main.py": "", "tests/test_one.py": "", "README.md": "", "Dockerfile": "RUN dangerous"},
    )
    with pytest.raises(PolicyError):
        builder.write_bundle(bundle, tmp_path)
