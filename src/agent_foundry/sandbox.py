import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

from .models import Manifest, validate_json
from .security import PolicyError

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
            import re

            digest = self.settings.approved_dependencies.get(dependency)
            if (
                not re.fullmatch(r"[a-zA-Z0-9_-]+==[0-9][a-zA-Z0-9.]*", dependency)
                or not digest
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise PolicyError("Dependency requires an approved version and wheel SHA256")
            requirements.append(f"{dependency} --hash=sha256:{digest}")
        files["requirements.txt"] = ("\n".join(requirements) + "\n").encode()
        files["_runner.py"] = WRAPPER.encode()
        # The model never controls Docker instructions or host commands.
        dockerfile = f"FROM {self.settings.sandbox_image}\nUSER 10001:10001\nWORKDIR /tool\n"
        dockerfile += "COPY --chown=10001:10001 . /tool/\n"
        if requirements:
            dockerfile += "RUN python -m pip install --user --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r requirements.txt\n"
        dockerfile += (
            'ENV PYTHONPATH=/tool PYTHONDONTWRITEBYTECODE=1\nENTRYPOINT ["python", "/tool/_runner.py"]\n'
        )
        files["Dockerfile"] = dockerfile.encode()
        archive = self.archive(files)
        tag = "foundry-tool:" + hashlib.sha256(archive).hexdigest()
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
        image = await self.commands.run(
            [self.settings.docker_binary, "image", "inspect", tag, "--format", "{{.Id}}"]
        )
        return image.decode().strip()

    async def run(self, image, manifest, data=None, command=None):
        if not image.startswith("sha256:"):
            raise PolicyError("Runtime requires an immutable image ID")
        name = "foundry-run-" + uuid4().hex
        limits = manifest.limits
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
            "none",
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
        if command:
            argv += ["--entrypoint", command[0], image, *command[1:]]
        else:
            argv.append(image)
        try:
            return await self.commands.run(
                argv,
                stdin=json.dumps(data or {}).encode(),
                timeout=min(limits.timeout_seconds, self.settings.execution_timeout),
            )
        finally:
            # Client timeout does not stop a daemon-side container; explicitly reap it.
            try:
                await self.commands.run([self.settings.docker_binary, "rm", "-f", name], timeout=10)
            except Exception:
                pass

    async def validate(self, image, manifest):
        if not manifest.examples:
            raise PolicyError("At least one schema-validated example with expected output is required")
        await self.run(
            image,
            manifest,
            command=["python", "-X", "pycache_prefix=/tmp/pycache", "-m", "py_compile", "app/main.py"],
        )
        output = await self.run(
            image,
            manifest,
            command=[
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=/tmp/pytest",
                "tests",
            ],
        )
        for example in manifest.examples:
            raw = await self.run(image, manifest, example.input)
            result = json.loads(raw)
            validate_json(result, manifest.output_schema)
            if result != example.output:
                raise PolicyError("Sample output differs from expected output")
            # Independent repeated execution catches common nondeterministic tools.
            if json.loads(await self.run(image, manifest, example.input)) != result:
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
        }
