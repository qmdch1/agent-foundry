import argparse
import asyncio
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from .config import Settings
from .container import Container
from .models import Manifest
from .security import PolicyError
from .seed import seed


async def execute(args):
    if args.command == "init-env":
        destination = Path(".env")
        if destination.exists():
            raise PolicyError(".env already exists; preserving it")
        password = secrets.token_urlsafe(24)
        text = Path(".env.example").read_text()
        replacements = {
            "CHANGE_DATABASE_PASSWORD": password,
            "CHANGE_API_KEY": secrets.token_urlsafe(32),
            "CHANGE_ADMIN_KEY": secrets.token_urlsafe(32),
            "CHANGE_HASH_KEY": secrets.token_urlsafe(32),
            "CHANGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }
        for before, after in replacements.items():
            text = text.replace(before, after)
        destination.write_text(text)
        destination.chmod(0o600)
        print("Created .env with separate random secrets; configure LLM models and credentials locally.")
        return
    container = Container(Settings())
    await container.open()
    try:
        match args.command:
            case "migrate":
                await container.db.migrate()
                await seed(container.registry)
                print("Migration and primitive seed completed")
            case "worker":
                await container.worker.run(once=args.once)
            case "web-login":
                token = await container.web_sessions.create_launch()
                print(args.url.rstrip("/") + "/#connect=" + token)
            case "catalog-sync":
                print(json.dumps(await container.catalog.sync()))
            case "reconcile":
                results = await container.deployment.reconcile()
                print(json.dumps(results))
                if any(not r["success"] for r in results):
                    raise SystemExit(1)
            case "rollback":
                await container.deployment.rollback(args.program_id, args.commit)
                print("Rollback completed")
            case "deploy":
                async with container.db.lock("tool-repository-deployment"):
                    directory = await container.deployment.checkout(
                        container.settings.tool_repository, args.commit, args.path
                    )
                    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
                    if manifest.runtime != "python":
                        raise PolicyError("Use register-api for HTTP adapters")
                    await container.deployment.deploy(manifest, directory, args.commit)
                    print(json.dumps({"program_id": str(manifest.program_id), "git_commit": args.commit}))
            case "register-api":
                manifest = Manifest.model_validate_json(Path(args.manifest).read_text())
                if manifest.runtime != "http" or not manifest.examples or manifest.side_effects:
                    raise PolicyError("Register a read-only HTTP adapter with sample health checks")
                from .models import validate_json

                for example in manifest.examples:
                    result = await container.executor.http(manifest, example.input)
                    validate_json(result, manifest.output_schema)
                await container.registry.register(manifest, status="ACTIVE", evidence={"passed": True})
                print(f"Registered {manifest.name}")
            case "enable-primitive":
                if args.name not in {"file-reader", "file-writer", "database-query"}:
                    raise PolicyError("This primitive cannot be activated directly")
                await container.db.execute(
                    """UPDATE agent.programs SET status='ACTIVE',updated_at=now(),
                    installed_at=COALESCE(installed_at,now()),last_deployed_at=now() WHERE name=%s""",
                    (args.name,),
                )
                await container.db.event("primitive_enabled", {"name": args.name})
                print("Administrator-only primitive enabled")
    finally:
        await container.close()


def main():
    parser = argparse.ArgumentParser(prog="foundry")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init-env", "migrate", "reconcile", "catalog-sync"):
        commands.add_parser(name)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("program_id")
    rollback.add_argument("commit")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("path")
    deploy.add_argument("commit")
    register = commands.add_parser("register-api")
    register.add_argument("manifest")
    primitive = commands.add_parser("enable-primitive")
    primitive.add_argument("name")
    web_login = commands.add_parser("web-login")
    web_login.add_argument("--url", default="http://localhost:8000")
    asyncio.run(execute(parser.parse_args()))
