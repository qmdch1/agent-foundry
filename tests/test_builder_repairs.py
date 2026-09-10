import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent_foundry.builder import Builder, repair_bundle
from agent_foundry.models import Manifest
from agent_foundry.security import PolicyError


def source():
    return {
        "manifest": Manifest(
            name="repair-example",
            version="1.0.0",
            description="Example deterministic repair",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ).model_dump(mode="json"),
        "files": {
            "app/main.py": "def run(data): return {}\n",
            "tests/test_main.py": "def test_run(): assert True\n",
            "README.md": "Example",
        },
    }


def test_repair_keeps_manifest_and_regression_tests(settings, tmp_path):
    previous = source()
    bundle = repair_bundle({"files": {"app/main.py": "def run(data): return data\n"}}, previous)
    assert bundle.files["tests/test_main.py"] == previous["files"]["tests/test_main.py"]
    assert bundle.manifest.model_dump(mode="json") == previous["manifest"]
    assert previous["files"]["app/main.py"] != bundle.files["app/main.py"]
    Builder(None, None, None, None, None, settings).write_bundle(bundle, tmp_path)
    assert (tmp_path / "app/main.py").read_text() == "def run(data): return data\n"


def test_repair_replaces_manifest_but_not_missing_files():
    previous = source()
    replacement = {**previous["manifest"], "version": "1.0.1"}
    bundle = repair_bundle({"manifest": replacement, "files": {}}, previous)
    assert bundle.manifest.version == "1.0.1"
    assert bundle.files == previous["files"]


@pytest.mark.parametrize("patch", [{"delete": ["tests/test_main.py"]}, {"files": []}])
def test_repair_rejects_unsupported_patch_operations(patch):
    with pytest.raises(PolicyError):
        repair_bundle(patch, source())


def test_repair_cannot_delete_files_or_escape_source_policy(settings, tmp_path):
    with pytest.raises(ValidationError):
        repair_bundle({"files": {"tests/test_main.py": None}}, source())
    bundle = repair_bundle({"files": {"../escape.py": "pass"}}, source())
    with pytest.raises(PolicyError):
        Builder(None, None, None, None, None, settings).write_bundle(bundle, tmp_path)
    assert not (tmp_path.parent / "escape.py").exists()


async def test_failed_build_retries_changed_file_with_all_validation(settings):
    @asynccontextmanager
    async def lock(_):
        yield

    previous = source()
    llm = SimpleNamespace(
        call=AsyncMock(
            side_effect=[
                previous,
                {"files": {"app/main.py": "def run(data): return data\n"}},
            ]
        )
    )
    db = SimpleNamespace(lock=lock, event=AsyncMock(), fetch=AsyncMock(return_value=[]))
    search = SimpleNamespace(search=AsyncMock(side_effect=[[], [object()]]))
    sandbox = SimpleNamespace(
        build=AsyncMock(side_effect=[PolicyError("sample failed"), "verified-image"]),
        validate=AsyncMock(),
    )
    builder = Builder(llm, search, SimpleNamespace(db=db), SimpleNamespace(sandbox=sandbox), None, settings)
    status, result = await builder.build({"capability": "Example deterministic repair"})
    assert status == "SKIPPED" and result["reason"] == "duplicate_at_publication"
    second = json.loads(llm.call.await_args_list[1].args[2])
    assert second["previous_bundle"] == previous
    assert second["previous_errors"] == ["sample failed"]
    assert sandbox.build.await_count == 2
    sandbox.validate.assert_awaited_once_with("verified-image", sandbox.build.await_args.args[1])
    repaired_path = sandbox.build.await_args.args[0]
    assert (repaired_path / "app/main.py").read_text() == "def run(data): return data\n"
    assert (repaired_path / "tests/test_main.py").read_text() == previous["files"]["tests/test_main.py"]
