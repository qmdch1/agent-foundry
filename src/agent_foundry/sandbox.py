import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from .models import Manifest, validate_json
from .security import PolicyError
from .timing import stage

WRAPPER = """import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("generated_tool", "/tool/app/main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
data = json.load(sys.stdin)
result = module.run(data)
if not isinstance(result, dict):
    raise TypeError("run() must return a dict")
print(json.dumps(result, allow_nan=False))
"""


class Sandbox:
    databases = None
    db = None

    def __init__(self, commands, settings):
        self.commands, self.settings = commands, settings

    def archive(self, files: dict[str, bytes]):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, content in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(content), 0o644, 0
                archive.addfile(info, io.BytesIO(content))
        return data.getvalue()

    async def build(self, directory: Path, manifest: Manifest):
        async with stage(self.db, "image_build", program_id=str(manifest.program_id)) as details:
            return await self._build(directory, manifest, details)

    async def inspect_image(self, name):
        image = await self.commands.run(
            [self.settings.docker_binary, "image", "inspect", name, "--format", "{{.Id}}"]
        )
        result = image.decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
            raise PolicyError("Docker returned an invalid immutable image ID")
        return result

    async def _build(self, directory, manifest, details):
        files = {}
        for p in directory.rglob("*"):
            if p.is_symlink():
                raise PolicyError("Tool archives cannot contain symlinks")
            if p.is_file() and (p.suffix == ".py" or p.name in {"manifest.json", "requirements.txt"}):
                files[p.relative_to(directory).as_posix()] = p.read_bytes()
        if sum(map(len, files.values())) > 2_000_000:
            raise PolicyError("Tool source size limit exceeded")
        requirements = []
        for dependency in manifest.dependencies:
            digest = self.settings.approved_dependencies.get(dependency)
            if (
                not re.fullmatch(r"[a-zA-Z0-9_-]+==[0-9][a-zA-Z0-9.]*", dependency)
                or not digest
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise PolicyError("Dependency requires an approved version and wheel SHA256")
            requirements.append(f"{dependency} --hash=sha256:{digest}")
        files["requirements.txt"] = ("\n".join(requirements) + "\n").encode()
        # Equivalent JSON formatting must not trigger a second build after Git publication.
        files["manifest.json"] = json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        files["_runner.py"] = WRAPPER.encode()
        base_image = await self.inspect_image(self.settings.sandbox_image)
        # BuildKit treats FROM sha256:<id> as a registry name, not a local image ID.
        # Bind a content-derived local alias to the inspected immutable image instead.
        base_tag = "foundry-base:" + base_image.removeprefix("sha256:")
        await self.commands.run([self.settings.docker_binary, "image", "tag", base_image, base_tag])
        # The model never controls Docker instructions or host commands.
        dockerfile = f"FROM {base_tag}\nUSER 10001:10001\nWORKDIR /tool\n"
        dockerfile += "COPY --chown=10001:10001 requirements.txt /tool/requirements.txt\n"
        if requirements:
            dockerfile += "RUN python -m pip install --user --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r requirements.txt\n"
        # Keep the installed dependency layer reusable when only Python source changes.
        dockerfile += "COPY --chown=10001:10001 . /tool/\n"
        dockerfile += (
            'ENV PYTHONPATH=/tool PYTHONDONTWRITEBYTECODE=1\nENTRYPOINT ["python", "/tool/_runner.py"]\n'
        )
        files["Dockerfile"] = dockerfile.encode()
        archive = self.archive(files)
        tag = "foundry-tool:" + hashlib.sha256(archive).hexdigest()
        try:
            image = await self.inspect_image(tag)
        except Exception:
            image = None
        if image:
            details["cache_hit"] = True
            return image
        details["cache_hit"] = False
        await self.commands.run(
            [
                self.settings.docker_binary,
                "build",
                "--network",
                "default" if requirements else "none",
                "--tag",
                tag,
                "-",
            ],
            stdin=archive,
            timeout=self.settings.build_timeout,
        )
        if await self.inspect_image(base_tag) != base_image:
            raise PolicyError("Sandbox base image changed during build")
        return await self.inspect_image(tag)

    async def run(self, image, manifest, data=None, command=None, *, database_env=None):
        if not image.startswith("sha256:"):
            raise PolicyError("Runtime requires an immutable image ID")
        name = "foundry-run-" + uuid4().hex
        limits = manifest.limits
        network = "none"
        if database_env:
            if not manifest.requires_db or set(database_env) != {
                "FOUNDRY_TOOL_DATABASE_URL",
                "FOUNDRY_TOOL_SCHEMA",
            }:
                raise PolicyError("Database runtime requires an explicit managed binding")
            network = self.settings.tool_database_network
            internal = await self.commands.run(
                [self.settings.docker_binary, "network", "inspect", network, "--format", "{{.Internal}}"]
            )
            if internal.decode().strip() != "true":
                raise PolicyError("Database tools require an internal Docker network")
        elif manifest.requires_db and not command:
            raise PolicyError("Database tool has no runtime binding")
        argv = [
            self.settings.docker_binary,
            "run",
            "--rm",
            "--name",
            name,
            "--init",
            "-i",
            "--user",
            "10001:10001",
            "--read-only",
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--log-driver",
            "none",
            "--memory",
            f"{min(limits.memory_mb, self.settings.memory_mb)}m",
            "--memory-swap",
            f"{min(limits.memory_mb, self.settings.memory_mb)}m",
            "--cpus",
            str(min(limits.cpu, self.settings.cpu)),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
        ]
        if database_env:
            # Docker reads these values from its client environment, never argv or audit logs.
            argv += ["--env", "FOUNDRY_TOOL_DATABASE_URL", "--env", "FOUNDRY_TOOL_SCHEMA"]
        if command:
            argv += ["--entrypoint", command[0], image, *command[1:]]
        else:
            argv.append(image)
        runtime_env = {**os.environ, **database_env} if database_env else None
        if runtime_env and os.name == "posix" and self.settings.docker_binary.endswith(".exe"):
            # WSL only forwards named WSLENV entries to the Windows Docker client.
            runtime_env["WSLENV"] = ":".join(filter(None, [runtime_env.get("WSLENV", ""), *database_env]))
        try:
            return await self.commands.run(
                argv,
                stdin=json.dumps(data or {}).encode(),
                timeout=min(limits.timeout_seconds, self.settings.execution_timeout),
                env=runtime_env,
            )
        finally:
            # Client timeout does not stop a daemon-side container; explicitly reap it.
            try:
                await self.commands.run([self.settings.docker_binary, "rm", "-f", name], timeout=10)
            except Exception:
                pass

    async def validate(self, image, manifest):
        async with stage(self.db, "validation", program_id=str(manifest.program_id)) as details:
            return await self._validate_cached(image, manifest, details)

    async def validation_key(self, image, manifest):
        if await self.inspect_image(image) != image:
            raise PolicyError("Validation image identity changed")
        # The complete trusted validator/database provisioning code and installed schema
        # libraries are part of policy, not just an easy-to-forget manual version.
        source = Path(__file__).parent
        policy = {
            "image": image,
            "base_image": await self.inspect_image(self.settings.sandbox_image),
            "manifest": manifest.model_dump(mode="json"),
            "implementation": {
                name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                for name in ("sandbox.py", "models.py", "program_databases.py", "security.py")
            },
            "libraries": {name: version(name) for name in ("jsonschema", "pydantic", "psycopg")},
            "settings": {
                name: getattr(self.settings, name)
                for name in (
                    "validation_policy_version",
                    "approved_dependencies",
                    "memory_mb",
                    "cpu",
                    "execution_timeout",
                    "max_output_bytes",
                    "build_timeout",
                    "docker_binary",
                    "tool_database_network",
                    "tool_database_host",
                    "tool_database_port",
                    "tool_database_connection_limit",
                    "tool_database_statement_timeout_ms",
                    "tool_database_lock_timeout_ms",
                )
            },
        }
        if manifest.requires_db and self.databases:
            rows = await self.databases.db.fetch("SELECT current_setting('server_version_num') AS version")
            policy["database_version"] = rows[0]["version"]
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()

    async def _validate_cached(self, image, manifest, details):
        key = await self.validation_key(image, manifest)
        cache_dir = self.settings.state_root / "validation-cache"
        if cache_dir.is_symlink():
            raise PolicyError("Validation cache cannot be a symlink")
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_path = cache_dir / (key + ".json")
        enabled = self.settings.validation_cache_enabled and self.settings.validation_cache_ttl_seconds > 0
        if enabled and cache_path.is_file() and not cache_path.is_symlink():
            try:
                entry = json.loads(cache_path.read_text())
                age = time.time() - entry["validated_at"]
                if (
                    entry["key"] == key
                    and entry["image"] == image
                    and 0 <= age < self.settings.validation_cache_ttl_seconds
                    and entry["evidence"]["passed"] is True
                ):
                    details["cache_hit"] = True
                    return {**entry["evidence"], "validation_cache_hit": True}
            except (ValueError, KeyError, TypeError, OSError):
                pass
        details["cache_hit"] = False
        evidence = await self._validate(image, manifest)
        if enabled:
            # Only the trusted control plane writes here; generated source is never a
            # cache authority and this directory is never mounted into tool containers.
            with tempfile.NamedTemporaryFile(mode="w", dir=cache_dir, delete=False) as stream:
                json.dump(
                    {"key": key, "image": image, "validated_at": time.time(), "evidence": evidence}, stream
                )
                temporary = Path(stream.name)
            temporary.replace(cache_path)
        return {**evidence, "validation_cache_hit": False}

    async def _validate(self, image, manifest):
        if not manifest.examples:
            raise PolicyError("At least one schema-validated example with expected output is required")
        await self.run(
            image,
            manifest,
            command=["python", "-X", "pycache_prefix=/tmp/pycache", "-m", "py_compile", "app/main.py"],
        )
        test_command = [
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp=/tmp/pytest",
            "tests",
        ]
        if manifest.requires_db:
            if not self.databases:
                raise PolicyError("Central database manager is not configured")
            async with self.databases.test_scope(manifest) as env:
                output = await self.run(image, manifest, command=test_command, database_env=env)
        else:
            output = await self.run(image, manifest, command=test_command)
        for example in manifest.examples:

            async def sample():
                if manifest.requires_db:
                    async with self.databases.test_scope(manifest) as env:
                        return await self.run(image, manifest, example.input, database_env=env)
                return await self.run(image, manifest, example.input)

            raw = await sample()
            result = json.loads(raw)
            validate_json(result, manifest.output_schema)
            if result != example.output:
                raise PolicyError("Sample output differs from expected output")
            # Independent repeated execution catches common nondeterministic tools.
            if json.loads(await sample()) != result:
                raise PolicyError("Sample output is not deterministic")
        return {
            "passed": True,
            "unit_tests": output.decode()[-2000:],
            "samples": len(manifest.examples),
            "syntax": True,
            "input_schema": True,
            "output_schema": True,
            "repeated_execution": True,
            "health": "CLI samples passed",
            "database_test_isolation": manifest.requires_db,
        }
