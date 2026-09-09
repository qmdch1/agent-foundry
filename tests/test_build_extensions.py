import pytest

from agent_foundry.build_extensions import apply_previous_tests, load_extension, validate_extension
from agent_foundry.models import Bundle, Example, Manifest, TableDefinition
from agent_foundry.security import PolicyError


@pytest.fixture
def manifest():
    return Manifest(
        name="query-records", version="1.0.0", description="Read and compare supplied records",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object"},
        examples=[Example(prompt="Find sample", input={"query": "sample"}, output={})],
    )


@pytest.fixture
def oldfiles():
    return {
        "app/main.py": "def run(data): return {}\n",
        "tests/test_main.py": "def test_existing(): assert True\n",
        "tests/conftest.py": "# retained fixture\n",
        "README.md": "Source README",
    }


def write_files(directory, files):
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def upgraded(manifest, **updates):
    data = manifest.model_dump()
    data.update(version="1.0.1")
    data.update(updates)
    return Manifest.model_validate(data)


def test_extension_loads_complete_sources_without_operational_files(tmp_path, manifest, oldfiles):
    write_files(tmp_path, oldfiles | {".env": "private", "tests/state.json": "private"})
    assert load_extension(tmp_path, manifest, 10000) == oldfiles
    exact_length = sum(map(len, oldfiles.values()))
    assert load_extension(tmp_path, manifest, exact_length) == oldfiles
    with pytest.raises(PolicyError, match="limit|UTF-8"):
        load_extension(tmp_path, manifest, exact_length - 1)


def test_extension_refuses_missing_tests_and_links(tmp_path, manifest, oldfiles):
    write_files(tmp_path, oldfiles)
    (tmp_path / "tests/test_main.py").unlink()
    with pytest.raises(PolicyError, match="regression"):
        load_extension(tmp_path, manifest, 10000)
    (tmp_path / "tests/test_main.py").symlink_to(tmp_path / "app/main.py")
    with pytest.raises(PolicyError, match="symlink"):
        load_extension(tmp_path, manifest, 10000)


def test_extension_refuses_secret_named_source(tmp_path, manifest, oldfiles):
    write_files(tmp_path, oldfiles | {"app/secrets.py": "password = 'private'"})
    with pytest.raises(PolicyError, match="Secret"):
        load_extension(tmp_path, manifest, 10000)


@pytest.mark.parametrize("update", [
    {"name": "other-tool"}, {"version": "1.0.0"}, {"version": "0.9.9"},
    {"input_schema": {"type": "object"}}, {"output_schema": {"type": "object", "maxProperties": 1}},
    {"visibility": "admin"}, {"side_effects": True}, {"dependencies": ["numpy==2.0.0"]},
    {"limits": {"memory_mb": 512}}, {"secret_name": "NEW_SECRET"},
])
def test_extension_rejects_incompatible_or_privileged_change(manifest, update):
    with pytest.raises(PolicyError):
        validate_extension(manifest, upgraded(manifest, **update))


def test_extension_accepts_version_and_new_command_without_contract_change(manifest):
    validate_extension(manifest, upgraded(manifest, description="Also find matching stored records"))
    validate_extension(upgraded(manifest, version="1.9.0"), upgraded(manifest, version="1.10.0"))


def test_extension_database_is_additive(manifest):
    old = upgraded(manifest, requires_db=True, network="database", tables=[
        TableDefinition(name="records", columns={"value": "jsonb"}, indexes=[["value"]])
    ])
    new = upgraded(old, version="1.1.0", tables=[
        TableDefinition(name="records", columns={"value": "jsonb", "category": "text"}, indexes=[["value"]]),
        TableDefinition(name="history", columns={"record": "text"}),
    ])
    validate_extension(old, new)
    for tables in (
        [TableDefinition(name="history", columns={"record": "text"})],
        [TableDefinition(name="records", columns={"value": "text"}, indexes=[["value"]])],
        [TableDefinition(name="records", columns={"value": "jsonb"})],
    ):
        with pytest.raises(PolicyError):
            validate_extension(old, upgraded(old, version="1.1.0", tables=tables))


def test_previous_tests_survive_conflicting_new_tests(manifest, oldfiles):
    newtest = "def test_new(): assert 1 == 1\n"
    bundle = Bundle(manifest=upgraded(manifest), files={"app/main.py": "new source", "tests/test_main.py": newtest})
    merged = apply_previous_tests(bundle, oldfiles, manifest)
    assert merged.files["tests/test_main.py"] == oldfiles["tests/test_main.py"]
    assert merged.files["tests/conftest.py"] == oldfiles["tests/conftest.py"]
    assert any(name.startswith("tests/test_extension_") and text == newtest for name, text in merged.files.items())
    assert bundle.files["tests/test_main.py"] == newtest


def test_fixture_changes_and_path_traversal_rejected(manifest, oldfiles):
    for files in (
        {"tests/conftest.py": "changed"}, {"tests/../app/main.py": "escape"},
        {"tests/new/conftest.py": "override"},
    ):
        with pytest.raises(PolicyError):
            apply_previous_tests(Bundle(manifest=upgraded(manifest), files=files), oldfiles, manifest)


def test_old_examples_always_survive_at_ten_example_bound(manifest, oldfiles):
    manifest.examples = [Example(prompt=f"Old {i}", input={"query": str(i)}, output={}) for i in range(10)]
    newer = upgraded(manifest, examples=[Example(prompt="New", input={"query": "new"}, output={})])
    merged = apply_previous_tests(Bundle(manifest=newer, files=oldfiles), oldfiles, manifest)
    assert merged.manifest.examples == manifest.examples
    assert newer.examples[0].prompt == "New"
