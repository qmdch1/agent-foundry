"""Inject trusted versioned helpers after LLM wrapper generation, before sandbox tests."""

import hashlib
from pathlib import Path

from .models import Bundle, TableDefinition
from .security import PolicyError

TEMPLATE_VERSION = "1.0.0"
NAMES = ("comparison", "aggregation", "storage")
STORAGE_TABLE = {
    "name": "template_records",
    "columns": {"record_key": "text", "payload": "jsonb"},
    "indexes": [["record_key"]],
}


def template_files(name: str) -> dict[str, str]:
    if name not in NAMES:
        raise PolicyError("Unknown build template")
    root = Path(__file__).parent / "template_sources"
    files = {"app/foundry_template.py": (root / f"{name}.py").read_text(encoding="utf-8")}
    if name == "comparison":
        files["app/foundry_storage.py"] = (root / "storage.py").read_text(encoding="utf-8")
    return files


def template_contract(name: str) -> dict:
    """Compact API only: no boilerplate source or user data in the LLM request."""
    files = template_files(name)
    signatures = {
        "comparison": [
            "from app.foundry_template import compare",
            "compare(records:list[dict], filters=[{field,op,value}], sort_by=None, descending=False, limit=100)",
            "op: eq (same JSON type) or numeric gte/lte/gt/lt; sort_by numeric; nulls last; "
            "returns {items,matched,total}; limit 1..1000, at most 10000 records and 20 filters",
            "from app.foundry_storage import store",
        ],
        "aggregation": [
            "from app.foundry_template import aggregate",
            "aggregate(records:list[dict], field:str, group_by:str|None=None)",
            "returns {groups:[{group,count,sum,average,min,max}],count}; "
            "numeric results are decimal strings, averages rounded to 50 significant digits; "
            "1..10000 records; missing/nonfinite values rejected; group values string or null",
        ],
        "storage": ["from app.foundry_template import store"],
    }
    persistent = name in {"comparison", "storage"}
    return {
        "name": name,
        "version": TEMPLATE_VERSION,
        "source_sha256": hashlib.sha256(
            "".join(path + source for path, source in sorted(files.items())).encode()
        ).hexdigest(),
        "reserved_files": list(files),
        "api": signatures[name],
        "storage_api": (
            'store({"operation":"put","key":"safe-id","value":{...}}) or '
            'store({"operation":"get","key":"safe-id"}) or '
            'store({"operation":"list","limit":50,"after":""}). '
            "Returns {items:[{key,value}],storage:{action,record_type}}. "
            "Use synthetic examples; persist supplied facts, sources, checked dates and result on comparison. "
            "Return the CURRENT store() storage outcome at top level, not historical stored metadata."
        )
        if persistent
        else None,
        "manifest_requirements": {
            "requires_db": persistent,
            "network": "database" if persistent else "none",
            "tables": [STORAGE_TABLE] if persistent else [],
        },
        "instruction": (
            "Write only the domain wrapper app/main.py, tests and README; import these helpers. "
            "Do not emit or replace reserved_files. The trusted Worker injects exact source before tests. "
            "Include independent normal, edge, invalid and persistence/replay tests; "
            "template use never replaces sandbox validation. CLI still implements stdin/stdout JSON."
        ),
    }


def apply_template(bundle: Bundle, name: str, version: str = TEMPLATE_VERSION) -> Bundle:
    """Copy, validate reserved source and add ordinary central-DB table declarations."""
    if version != TEMPLATE_VERSION:
        raise PolicyError("Unsupported build template version")
    files = template_files(name)
    result = bundle.model_copy(deep=True)
    for path, source in files.items():
        if path in result.files and result.files[path] != source:
            raise PolicyError("Generated code must not override trusted template files")
        result.files[path] = source
    if name in {"comparison", "storage"}:
        expected = TableDefinition.model_validate(STORAGE_TABLE)
        existing = [table for table in result.manifest.tables if table.name == expected.name]
        if existing and (len(existing) != 1 or existing[0] != expected):
            raise PolicyError("Template storage table conflicts with manifest")
        if not existing:
            result.manifest.tables.append(expected)
        result.manifest.requires_db = True
        result.manifest.network = "database"
    return Bundle.model_validate(result.model_dump())
