"""Conservative, bounded source reuse for automatic Python tool upgrades.

An upgrade keeps the existing JSON contracts exactly. New commands within an
existing string/object contract are supported; schema redesign needs an explicit
migration. Sources are read as text only and are executed only by the sandbox.
"""

import hashlib
import os
import re
from pathlib import Path, PurePosixPath

from .models import Bundle, Manifest
from .security import PolicyError, safe_path


def _source_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        name == path.as_posix()
        and ".." not in path.parts
        and bool(re.fullmatch(r"(?:app|tests)/[a-zA-Z0-9_/]+\.py|README\.md", name))
    )


def _test_name(name: str) -> bool:
    return name.startswith("tests/") and PurePosixPath(name).name.startswith("test_")


def load_extension(directory: Path, manifest: Manifest, max_chars: int) -> dict[str, str]:
    """Read complete public source, never a truncated source or operational file.

    Refuse oversized bundles and symlinks. The caller must supply a checkout of
    the recorded immutable revision, not a mutable installed runtime directory.
    """
    if max_chars <= 0:
        raise PolicyError("Extension source limit must be positive")
    if directory.is_symlink() or not directory.is_dir():
        raise PolicyError("Extension requires a real source directory")
    if manifest.runtime != "python":
        raise PolicyError("Only Python tools support automatic extension")
    names = ["README.md"]
    for folder in ("app", "tests"):
        base = safe_path(directory, folder)
        if not base.is_dir():
            raise PolicyError("Extension requires app and tests directories")
        for current, dirs, files in os.walk(base, followlinks=False):
            for part in dirs + files:
                if (Path(current) / part).is_symlink():
                    raise PolicyError("Extension source cannot contain symlinks")
            for part in files:
                path = Path(current) / part
                name = path.relative_to(directory.resolve()).as_posix()
                if path.suffix == ".py":
                    if not _source_name(name):
                        raise PolicyError("Unsupported extension source path")
                    if path.stem.lower() in {"secrets", "credentials", "passwords", "tokens"}:
                        raise PolicyError("Secret-bearing source files cannot enter extension context")
                    names.append(name)
                    if len(names) > 30:
                        raise PolicyError("Extension source has too many files")
    if manifest.entrypoint not in names or not any(_test_name(name) for name in names):
        raise PolicyError("Extension requires its entrypoint and regression tests")
    files = {}
    remaining = max_chars
    for name in sorted(names):
        path = safe_path(directory, name)
        if not path.is_file():
            raise PolicyError("Extension requires complete source and README")
        with path.open("rb") as stream:
            data = stream.read(remaining * 4 + 1)
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError("Extension source must be bounded UTF-8 text") from exc
        remaining -= len(source)
        if remaining < 0:
            raise PolicyError("Extension source exceeds configured context limit")
        files[name] = source
    return files


def validate_extension(old_manifest: Manifest, new_manifest: Manifest) -> None:
    """Reject identity, contract, privileges or destructive database changes."""
    unchanged = (
        "name", "runtime", "entrypoint", "execution_type", "visibility", "endpoint", "method",
        "secret_name", "network", "requires_db", "side_effects", "input_schema", "output_schema",
    )
    for field in unchanged:
        if getattr(old_manifest, field) != getattr(new_manifest, field):
            raise PolicyError(f"Automatic extension must preserve {field}")
    if old_manifest.program_id != new_manifest.program_id:
        raise PolicyError("Automatic extension must preserve program identity")
    if tuple(map(int, new_manifest.version.split("."))) <= tuple(map(int, old_manifest.version.split("."))):
        raise PolicyError("Extension version must increase")
    if set(new_manifest.dependencies) != set(old_manifest.dependencies):
        raise PolicyError("Automatic extension cannot change approved dependencies")
    for limit in ("timeout_seconds", "memory_mb", "cpu"):
        if getattr(new_manifest.limits, limit) > getattr(old_manifest.limits, limit):
            raise PolicyError("Automatic extension cannot increase resource limits")
    for manifest in (old_manifest, new_manifest):
        if len({table.name for table in manifest.tables}) != len(manifest.tables):
            raise PolicyError("Extension database table names must be unambiguous")
    new_tables = {table.name: table for table in new_manifest.tables}
    for table in old_manifest.tables:
        newer = new_tables.get(table.name)
        if newer is None or any(newer.columns.get(k) != v for k, v in table.columns.items()):
            raise PolicyError("Extension must retain existing database tables and column types")
        if any(index not in newer.indexes for index in table.indexes):
            raise PolicyError("Automatic extension must retain existing indexes")
    if any(rule not in new_manifest.selection_rules for rule in old_manifest.selection_rules):
        raise PolicyError("Automatic extension must retain existing selection rules")


def apply_previous_tests(bundle: Bundle, oldfiles: dict[str, str], oldmanifest: Manifest) -> Bundle:
    """Return an independent bundle retaining all old tests, helpers and examples.

    New test modules colliding with an old module receive a stable unique name.
    Changed fixtures/helpers are rejected, because silently overwriting them
    could invalidate the old regression tests. Examples retain their old order;
    only new examples fitting the ten-example manifest bound are appended.
    """
    if any(not _source_name(name) for name in (*oldfiles, *bundle.files)):
        raise PolicyError("Extension contains unsupported source paths")
    if not any(_test_name(name) for name in oldfiles):
        raise PolicyError("Automatic extension requires existing regression tests")
    if any(
        name.startswith("tests/") and PurePosixPath(name).name == "conftest.py"
        and name not in oldfiles for name in bundle.files
    ):
        raise PolicyError("Automatic extension cannot introduce regression fixture overrides")
    result = bundle.model_copy(deep=True)
    for name, original in oldfiles.items():
        if not name.startswith("tests/"):
            continue
        changed = result.files.get(name)
        if changed is not None and changed != original:
            if not _test_name(name):
                raise PolicyError("Automatic extension cannot replace regression fixtures or helpers")
            digest = hashlib.sha256((name + "\0" + changed).encode()).hexdigest()[:16]
            newname = str(PurePosixPath(name).with_name(f"test_extension_{digest}.py"))
            if newname in oldfiles or newname in result.files:
                raise PolicyError("Extension test filename collides with existing source")
            result.files[newname] = changed
        result.files[name] = original
    examples = list(oldmanifest.examples)
    for example in result.manifest.examples:
        if len(examples) >= 10:
            break
        if example not in examples:
            examples.append(example)
    result.manifest.examples = examples
    if len(result.files) > 30 or sum(len(v.encode()) for v in result.files.values()) > 500_000:
        raise PolicyError("Extension plus regression tests exceeds bundle limits")
    return result
