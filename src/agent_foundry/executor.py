import asyncio
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlsplit

import httpx

from .models import Manifest, validate_json
from .primitives import builtin
from .program_databases import ProgramDatabases
from .security import PolicyError


def resolve_input(value, results):
    if isinstance(value, dict):
        if "$from_step" in value:
            result = results[value["$from_step"]]
            for part in value["path"]:
                result = result[part]
            return result
        return {k: resolve_input(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_input(v, results) for v in value]
    return value


class Executor:
    def __init__(self, registry, sandbox, settings):
        self.registry, self.sandbox, self.settings = registry, sandbox, settings
        self.semaphore = asyncio.Semaphore(settings.executor_concurrency)
        self.databases = ProgramDatabases(registry.db, settings)

    async def http(self, manifest, data):
        endpoint = urlsplit(manifest.endpoint or "")
        if (
            endpoint.scheme != "https"
            or endpoint.hostname not in self.settings.http_allowed_hosts
            or endpoint.username
            or endpoint.password
            or endpoint.fragment
            or endpoint.query
            or endpoint.port not in (None, 443)
        ):
            raise PolicyError("Endpoint is outside the approved HTTPS allowlist")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            endpoint.hostname, 443, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise PolicyError("Private, loopback and metadata endpoints are prohibited")
        headers = {}
        if manifest.secret_name:
            secret = os.environ.get(manifest.secret_name)
            if not secret:
                raise PolicyError("Required secret reference is unavailable")
            headers["Authorization"] = f"Bearer {secret}"
        async with httpx.AsyncClient(
            timeout=self.settings.execution_timeout, follow_redirects=False, trust_env=False
        ) as client:
            kwargs = {"params": data} if manifest.method == "GET" else {"json": data}
            async with client.stream(
                manifest.method, manifest.endpoint, headers=headers, **kwargs
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.max_output_bytes:
                        raise PolicyError("HTTP response exceeds output limit")
                return json.loads(body)

    async def execute(self, program_id, data, request_id, *, admin=False):
        async with self.semaphore:
            row = await self.registry.get(program_id)
            manifest = Manifest.model_validate(row["manifest"])
            if (manifest.visibility != "public" or manifest.side_effects) and not admin:
                raise PolicyError("This tool requires administrator authorization")
            validate_json(data, manifest.input_schema)
            started, success, error = time.monotonic(), False, None
            try:
                async with asyncio.timeout(self.settings.execution_timeout + 15):
                    if manifest.runtime == "builtin":
                        result = await builtin(manifest.name, data, self.settings)
                    elif manifest.runtime == "http":
                        result = await self.http(manifest, data)
                    else:
                        receipt_path = (
                            self.settings.state_root
                            / "deployments"
                            / str(program_id)
                            / f"{row['git_commit']}.json"
                        )
                        if not receipt_path.is_file():
                            raise PolicyError("Pinned release is not deployed here; run reconcile")
                        receipt = json.loads(receipt_path.read_text())
                        if receipt["manifest"] != row["manifest"]:
                            raise PolicyError("Registry and deployed manifest disagree")
                        env = await self.databases.runtime(program_id) if manifest.requires_db else None
                        raw = await self.sandbox.run(receipt["image"], manifest, data, database_env=env)
                        result = json.loads(raw)
                    validate_json(result, manifest.output_schema)
                    if len(json.dumps(result).encode()) > self.settings.max_output_bytes:
                        raise PolicyError("Tool output exceeds limit")
                    success = True
                    return result
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                duration = (time.monotonic() - started) * 1000
                await self.registry.record_execution(program_id, success, duration)
                await self.registry.db.event(
                    "tool_execution",
                    {
                        "program_id": str(program_id),
                        "version": row["version"],
                        "git_commit": row["git_commit"],
                        "duration_ms": duration,
                        "success": success,
                        "error": error,
                    },
                    request_id,
                )

    async def plan(self, plan, request_id):
        results = {}
        for step in plan.programs:
            data = resolve_input(step.input, results)
            results[step.order] = await self.execute(step.program_id, data, request_id)
        return results[len(plan.programs)]
