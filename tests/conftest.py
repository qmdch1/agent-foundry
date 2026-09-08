import os

import pytest
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict

from agent_foundry.config import Settings
from agent_foundry.container import Container
from agent_foundry.seed import seed


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        state_root=tmp_path / "state",
        file_root=tmp_path / "files",
        tool_repository_root=tmp_path / "tools",
        api_key="test-user",
        admin_key="test-admin",
        prompt_hash_key="test-hash",
        job_encryption_key=Fernet.generate_key().decode(),
        main_model="test-main",
        router_model="test-router",
        evaluator_model="test-evaluator",
        builder_model="test-builder",
        evaluation_delay_seconds=0,
    )


@pytest.fixture
async def container(settings):
    dsn = os.environ.get("FOUNDRY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set FOUNDRY_TEST_DATABASE_URL to run PostgreSQL integration tests")
    if conninfo_to_dict(dsn).get("dbname") != "foundry_test":
        pytest.fail("Integration tests only accept the isolated foundry_test database")
    from pydantic import SecretStr

    settings.database_url = SecretStr(dsn)
    app = Container(settings)
    await app.open()
    await app.db.migrate()
    for table in (
        "programs",
        "releases",
        "jobs",
        "events",
        "tool_migrations",
        "console_settings",
        "web_sessions",
        "web_launches",
    ):
        await app.db.execute(f"DELETE FROM agent.{table}")
    await seed(app.registry)
    yield app
    await app.close()
