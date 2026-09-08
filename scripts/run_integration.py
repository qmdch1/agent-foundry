"""Run tests only against the explicitly named, isolated foundry_test database."""

import os
import sys

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_foundry.config import Settings

settings = Settings()
connection = conninfo_to_dict(settings.database_url.get_secret_value())
connection["dbname"] = "foundry_test"
os.environ["FOUNDRY_TEST_DATABASE_URL"] = make_conninfo(**connection)
raise SystemExit(pytest.main(["-q", *sys.argv[1:]]))
