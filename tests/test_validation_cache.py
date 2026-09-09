import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_foundry.deployment import Deployment
from agent_foundry.models import Manifest
from agent_foundry.sandbox import Sandbox
from agent_foundry.timing import stage


class Docker:
    def __init__(self):
        self.base = "sha256:" + "a" * 64
        self.images = {}
        self.builds = []

    async def run(self, args, **kwargs):
        if args[1:3] == ["image", "tag"]:
            self.images[args[4]] = args[3]
            return b""
        if args[1:3] == ["image", "inspect"]:
            name = args[3]
            if name == "agent-foundry-sandbox:0.1.0":
                return self.base.encode()
            if name in self.images:
                return self.images[name].encode()
            if name in self.images.values():
                return name.encode()
            raise RuntimeError("image missing")
        if args[1] == "build":
            self.builds.append(kwargs["stdin"])
            self.images[args[args.index("--tag") + 1]] = (
                "sha256:" + hashlib.sha256(kwargs["stdin"]).hexdigest()
            )
            return b""
        raise AssertionError(args)


@pytest.fixture
def tool(tmp_path):
    root = tmp_path / "source"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app/main.py").write_text("def run(data): return data\n")
    (root / "tests/test_main.py").write_text("def test_sample(): assert True\n")
    manifest = Manifest(
        name="cache-test",
        version="1.0.0",
        description="Cache validation test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        examples=[{"prompt": "test", "input": {}, "output": {}}],
    )
    (root / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    return root, manifest


async def test_published_copy_reuses_image_and_successful_validation(settings, tool, tmp_path):
    import shutil

    root, manifest = tool
    docker = Docker()
    sandbox = Sandbox(docker, settings)
    sandbox._validate = AsyncMock(return_value={"passed": True, "samples": 1})
    first = await sandbox.build(root, manifest)
    assert not (await sandbox.validate(first, manifest))["validation_cache_hit"]
    checkout = tmp_path / "checkout"
    shutil.copytree(root, checkout)
    (checkout / "manifest.json").write_text(manifest.model_dump_json())
    (checkout / "generation_tokens.txt").write_text("1000\n")
    second = await sandbox.build(checkout, manifest)
    assert second == first and len(docker.builds) == 1
    # Cache survives Worker process restarts; it is outside the generated checkout.
    other = Sandbox(docker, settings)
    other._validate = AsyncMock(side_effect=AssertionError("must reuse validation"))
    assert (await other.validate(second, manifest))["validation_cache_hit"]
    sandbox._validate.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    ["source", "test", "manifest", "base", "dependency", "policy", "limits", "expiry", "disabled", "corrupt"],
)
async def test_changed_inputs_or_stale_cache_revalidate(settings, tool, change):
    root, manifest = tool
    docker = Docker()
    sandbox = Sandbox(docker, settings)
    sandbox._validate = AsyncMock(return_value={"passed": True})
    image = await sandbox.build(root, manifest)
    await sandbox.validate(image, manifest)
    if change == "source":
        (root / "app/main.py").write_text("def run(data): return dict(data)\n")
    elif change == "test":
        (root / "tests/test_main.py").write_text("def test_new(): assert 2 == 2\n")
    elif change == "manifest":
        manifest.examples[0].prompt = "different example"
    elif change == "base":
        docker.base = "sha256:" + "b" * 64
    elif change == "dependency":
        manifest.dependencies = ["example==1.0"]
        settings.approved_dependencies = {"example==1.0": "b" * 64}
    elif change == "policy":
        settings.validation_policy_version = "next"
    elif change == "limits":
        settings.memory_mb = 512
    elif change == "expiry":
        path = next((settings.state_root / "validation-cache").glob("*.json"))
        entry = json.loads(path.read_text())
        entry["validated_at"] = 0
        path.write_text(json.dumps(entry))
    elif change == "disabled":
        settings.validation_cache_enabled = False
    else:
        next((settings.state_root / "validation-cache").glob("*.json")).write_text("invalid")
    image = await sandbox.build(root, manifest)
    assert not (await sandbox.validate(image, manifest))["validation_cache_hit"]
    assert sandbox._validate.await_count == 2


async def test_removed_image_is_rebuilt_and_failed_validation_never_cached(settings, tool):
    root, manifest = tool
    docker = Docker()
    sandbox = Sandbox(docker, settings)
    sandbox._validate = AsyncMock(side_effect=ValueError("failed tests"))
    image = await sandbox.build(root, manifest)
    for _ in range(2):
        with pytest.raises(ValueError, match="failed tests"):
            await sandbox.validate(image, manifest)
    assert sandbox._validate.await_count == 2
    docker.images.clear()
    await sandbox.build(root, manifest)
    assert len(docker.builds) == 2
    assert not list((settings.state_root / "validation-cache").glob("*.json"))


async def test_generated_files_cannot_supply_cache_and_dependencies_precede_source(settings, tool):
    root, manifest = tool
    (root / "validation-cache").mkdir()
    (root / "validation-cache/forged.json").write_text('{"passed":true}')
    manifest.dependencies = ["example==1.0"]
    settings.approved_dependencies = {"example==1.0": "b" * 64}
    docker = Docker()
    sandbox = Sandbox(docker, settings)
    sandbox._validate = AsyncMock(return_value={"passed": True})
    image = await sandbox.build(root, manifest)
    assert not (await sandbox.validate(image, manifest))["validation_cache_hit"]
    with tarfile.open(fileobj=io.BytesIO(docker.builds[0])) as archive:
        assert "validation-cache/forged.json" not in archive.getnames()
        dockerfile = archive.extractfile("Dockerfile").read().decode()
    assert "foundry-base:" + docker.base.removeprefix("sha256:") in dockerfile
    assert (
        dockerfile.index("requirements.txt /tool/")
        < dockerfile.index("RUN python")
        < dockerfile.index(". /tool/")
    )


async def test_deployment_cache_hit_still_runs_live_migration_health(settings, tool):
    root, manifest = tool
    database = SimpleNamespace(event=AsyncMock())
    registry = SimpleNamespace(db=database, register=AsyncMock())
    sandbox = SimpleNamespace(
        build=AsyncMock(return_value="sha256:" + "b" * 64),
        validate=AsyncMock(return_value={"passed": True, "validation_cache_hit": True}),
    )
    deployment = Deployment(registry, sandbox, None, settings)
    deployment.apply_tables = AsyncMock()
    for commit in ("a" * 40, "b" * 40):
        receipt = await deployment.deploy(manifest, root, commit)
        assert receipt["image"] == "sha256:" + "b" * 64
    assert deployment.apply_tables.await_count == 2
    deployment.apply_tables.side_effect = ValueError("database unavailable")
    with pytest.raises(ValueError, match="database unavailable"):
        await deployment.deploy(manifest, root, "c" * 40)
    assert registry.register.await_count == 2
    assert not (
        settings.state_root / "deployments" / str(manifest.program_id) / ("c" * 40 + ".json")
    ).exists()


async def test_stage_records_failure_without_request_contents():
    db = SimpleNamespace(event=AsyncMock())
    with pytest.raises(ValueError):
        async with stage(db, "validation", program_id="id"):
            raise ValueError("private request contents")
    args = db.event.call_args.args
    assert args[0] == "pipeline_stage"
    assert args[1]["success"] is False and args[1]["duration_ms"] >= 0
    assert args[1]["error_type"] == "ValueError"
    assert "private" not in json.dumps(args[1])
