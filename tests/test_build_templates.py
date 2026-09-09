import pytest

from agent_foundry.build_templates import apply_template, template_contract, template_files
from agent_foundry.models import Bundle, Manifest
from agent_foundry.security import PolicyError
from agent_foundry.template_sources.aggregation import aggregate
from agent_foundry.template_sources.comparison import compare


def bundle():
    return Bundle(
        manifest=Manifest(
            name="sample-template",
            version="1.0.0",
            description="Synthetic data tool",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        files={
            "app/main.py": "def run(data): return data\n",
            "tests/test_main.py": "",
            "README.md": "Synthetic tool",
        },
    )


def test_comparison_numeric_filter_sort_and_missing():
    rows = [
        {"name": "A", "water": 200, "weight": "51.2"},
        {"name": "B", "water": 100, "weight": 10},
        {"name": "C", "water": 200},
        {"name": "D", "water": 200, "weight": "40.1"},
    ]
    result = compare(rows, [{"field": "water", "op": "gte", "value": 200}], "weight", limit=2)
    assert [row["name"] for row in result["items"]] == ["D", "A"]
    assert result["matched"] == 3 and result["total"] == 4
    assert compare(rows, sort_by="weight", descending=True)["items"][-1]["name"] == "C"


def test_comparison_does_not_conflate_boolean_integer_or_missing():
    rows = [{"value": True}, {"value": 1}, {}]
    assert compare(rows, [{"field": "value", "op": "eq", "value": 1}])["items"] == [{"value": 1}]


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "1e10000", {}, None])
def test_invalid_comparison_numbers(value):
    with pytest.raises(ValueError):
        compare([{"number": value}], [{"field": "number", "op": "gte", "value": value}])


def test_decimal_aggregation_and_grouping():
    result = aggregate(
        [{"team": "A", "amount": "0.1"}, {"team": "A", "amount": "0.2"}, {"team": "B", "amount": "-1.2"}],
        "amount",
        "team",
    )
    assert result["count"] == 3
    assert result["groups"][0] == {
        "group": "A",
        "count": 2,
        "sum": "0.3",
        "average": "0.15",
        "min": "0.1",
        "max": "0.2",
    }
    assert result["groups"][1]["sum"] == "-1.2"


@pytest.mark.parametrize("rows", [[], [{}], [{"n": True}], [{"n": "NaN"}], [{"n": "1e10000"}]])
def test_aggregation_rejects_invalid_data(rows):
    with pytest.raises(ValueError):
        aggregate(rows, "n")


@pytest.mark.parametrize("name", ["comparison", "aggregation", "storage"])
def test_trusted_injection_version_and_immutability(name):
    original = bundle()
    result = apply_template(original, name)
    assert original.files == bundle().files
    assert result.manifest.requires_db == (name != "aggregation")
    assert apply_template(result, name) == result
    contract = template_contract(name)
    assert contract["version"] == "1.0.0" and len(contract["source_sha256"]) == 64
    for path, source in template_files(name).items():
        compile(source, path, "exec")
    with pytest.raises(PolicyError):
        apply_template(original, name, "999.0.0")
    original.files["app/foundry_template.py"] = "malicious override"
    with pytest.raises(PolicyError):
        apply_template(original, name)


async def test_storage_template_real_database(container, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from agent_foundry.template_sources.storage import store

    manifest = apply_template(bundle(), "storage").manifest
    base = conninfo_to_dict(container.settings.database_url.get_secret_value())
    container.settings.tool_database_host = base["host"]
    container.settings.tool_database_port = int(base["port"])
    async with container.deployment.databases.test_scope(manifest) as environment:
        monkeypatch.setenv("FOUNDRY_TOOL_DATABASE_URL", environment["FOUNDRY_TOOL_DATABASE_URL"])
        assert (
            store({"operation": "put", "key": "sample", "value": {"n": 1}})["storage"]["action"] == "created"
        )
        assert (
            store({"operation": "put", "key": "sample", "value": {"n": 1}})["storage"]["action"] == "reused"
        )
        assert (
            store({"operation": "put", "key": "sample", "value": {"n": 2}})["storage"]["action"] == "updated"
        )
        result = store({"operation": "get", "key": "sample"})
        assert result["storage"]["action"] == "read"
        assert result["items"] == [{"key": "sample", "value": {"n": 2}}]
        assert store({"operation": "list", "after": "sample"})["items"] == []
