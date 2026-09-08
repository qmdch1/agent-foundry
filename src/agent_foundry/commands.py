import asyncio
import hashlib
import os
import time

from .security import PolicyError, mask


class Commands:
    """Argument-vector-only privileged operations with bounded output and durable audit."""

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def run(self, argv, *, cwd=None, stdin=b"", timeout=None, limit=None, env=None):
        argv = [str(a) for a in argv]
        if argv[0] == "git" and os.environ.get("FOUNDRY_GIT_TOKEN"):
            argv[1:1] = [
                "-c",
                "credential.helper=",
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "credential.helper=!python -m agent_foundry.git_credentials",
            ]
        operation_id = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
        await self.db.event("privileged_command_started", {"operation_id": operation_id, "argv": argv})
        started, success = time.monotonic(), False
        process = None
        cap = limit or self.settings.max_output_bytes

        async def read(stream):
            data = bytearray()
            while chunk := await stream.read(65536):
                data.extend(chunk)
                if len(data) > cap:
                    raise PolicyError("Command output limit exceeded")
            return bytes(data)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def send():
                try:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

            async with asyncio.timeout(timeout or self.settings.execution_timeout):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(send())
                    out = tasks.create_task(read(process.stdout))
                    err = tasks.create_task(read(process.stderr))
                    tasks.create_task(process.wait())
                if process.returncode:
                    raise PolicyError(
                        mask((err.result() + out.result()).decode(errors="replace")[-4000:])
                        or f"Command exited with status {process.returncode}"
                    )
                success = True
                return out.result()
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            await self.db.event(
                "privileged_command_finished",
                {
                    "operation_id": operation_id,
                    "success": success,
                    "duration_ms": (time.monotonic() - started) * 1000,
                },
            )
