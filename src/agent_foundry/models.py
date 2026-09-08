import json
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    timeout_seconds: float = Field(30, gt=0, le=300)
    memory_mb: int = Field(256, ge=64, le=4096)
    cpu: float = Field(1, gt=0, le=8)


class Example(StrictModel):
    prompt: str = Field(max_length=500)
    input: dict[str, Any]
    output: dict[str, Any]


class SelectionRule(StrictModel):
    pattern: str = Field(max_length=500)
    fields: dict[str, Literal["string", "integer", "number", "json"]] = {}

    @field_validator("pattern")
    @classmethod
    def anchored(cls, value):
        import regex

        if not value.startswith("^") or not value.endswith("$"):
            raise ValueError("Selection patterns must be fully anchored")
        regex.compile(value)
        return value


class TableDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    columns: dict[str, Literal["text", "integer", "bigint", "numeric", "boolean", "jsonb", "timestamptz"]]
    indexes: list[list[str]] = []

    @model_validator(mode="after")
    def valid_columns(self):
        import re

        if not self.columns or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", c) for c in self.columns):
            raise ValueError("Invalid column name")
        if any(not index or any(c not in self.columns for c in index) for index in self.indexes):
            raise ValueError("Index refers to an unknown column")
        return self


class Manifest(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,47}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=5, max_length=1500)
    runtime: Literal["python", "http", "builtin"] = "python"
    execution_type: Literal["process", "docker", "http", "builtin"] = "process"
    entrypoint: str = "app/main.py"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tags: list[str] = Field(default_factory=list, max_length=20)
    examples: list[Example] = Field(default_factory=list, max_length=10)
    selection_rules: list[SelectionRule] = Field(default_factory=list, max_length=10)
    limits: Limits = Field(default_factory=Limits)
    requires_db: bool = False
    tables: list[TableDefinition] = Field(default_factory=list, max_length=10)
    network: Literal["none", "database"] = "none"
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    endpoint: str | None = None
    method: Literal["GET", "POST"] = "GET"
    secret_name: str | None = Field(None, pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    visibility: Literal["public", "admin", "internal"] = "public"
    side_effects: bool = False
    generation_tokens_estimated: bool = False

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_schema(cls, value):
        if len(json.dumps(value)) > 16000 or value.get("type") != "object":
            raise ValueError("Schemas must be bounded JSON object schemas")

        def check(node, depth=0):
            if depth > 12:
                raise ValueError("Schema nesting exceeds the supported bound")
            if isinstance(node, dict):
                if any(key in node for key in ("$ref", "$dynamicRef", "pattern", "patternProperties")):
                    raise ValueError(
                        "Runtime schemas must be nonrecursive and omit regular-expression keywords"
                    )
                for item in node.values():
                    check(item, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    check(item, depth + 1)

        check(value)
        Draft202012Validator.check_schema(value)
        return value

    @model_validator(mode="after")
    def valid_manifest(self):
        path = PurePosixPath(self.entrypoint)
        if path.is_absolute() or ".." in path.parts or "\\" in self.entrypoint:
            raise ValueError("Entrypoint must be repository-relative")
        if self.runtime == "python" and (
            self.entrypoint != "app/main.py" or self.execution_type not in {"process", "docker"}
        ):
            raise ValueError("Python tools use app/main.py in an isolated container")
        if self.runtime == "http" and (not self.endpoint or self.execution_type != "http"):
            raise ValueError("HTTP programs require an administrator-provided endpoint")
        if self.requires_db != bool(self.tables):
            raise ValueError("requires_db must match declarative table definitions")
        if self.network == "database" and not self.requires_db:
            raise ValueError("The database network requires declared program tables")
        if self.requires_db and self.runtime != "python":
            raise ValueError("Managed program schemas are supported by Python tools")
        for example in self.examples:
            validate_json(example.input, self.input_schema)
            validate_json(example.output, self.output_schema)
        return self

    @property
    def program_id(self) -> UUID:
        return uuid5(NAMESPACE_URL, f"agent-foundry:tool:{self.name}")


def validate_json(value, schema):
    json.dumps(value, allow_nan=False)
    Draft202012Validator(schema).validate(value)


class Candidate(StrictModel):
    program_id: UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    tags: list[str]
    examples: list[Example]
    score: float = Field(ge=0, le=1)
    mapped_input: dict[str, Any] | None = Field(None, exclude=True)


class Step(StrictModel):
    program_id: UUID
    order: int = Field(ge=1)
    input: dict[str, Any] = {}


class Plan(StrictModel):
    action: Literal["execute", "none"]
    programs: list[Step] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_plan(self):
        if self.action == "none" and self.programs or self.action == "execute" and not self.programs:
            raise ValueError("Action and programs disagree")
        if [s.order for s in self.programs] != list(range(1, len(self.programs) + 1)):
            raise ValueError("Steps must have contiguous ascending order")
        return self


class Evaluation(StrictModel):
    reuse_score: float = Field(ge=0, le=1)
    determinism_score: float = Field(ge=0, le=1)
    token_saving_score: float = Field(ge=0, le=1)
    latency_saving_score: float = Field(ge=0, le=1)
    accuracy_gain_score: float = Field(ge=0, le=1)
    specialized_data_score: float = Field(ge=0, le=1)
    maintenance_cost: float = Field(ge=0, le=1)
    estimated_saved_tokens_per_use: int = Field(ge=0, le=1_000_000)
    capability: str = Field(min_length=5, max_length=1000)
    reason: str = Field(max_length=1000)


class Bundle(StrictModel):
    manifest: Manifest
    files: dict[str, str]


class AgentRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=50000)
    explain_result: bool = False
    allow_build: bool = True
