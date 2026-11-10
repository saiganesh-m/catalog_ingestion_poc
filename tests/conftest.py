"""
Shared pytest setup: every test run uses a throwaway SQLite database and the mock JEV engine,
so tests never touch catalog.db and never call the live API.
"""
import os
import tempfile
from pathlib import Path

# Must run before any `app` import: settings and the database engine are created at import time.
_TEST_DIR = Path(tempfile.mkdtemp(prefix="jev_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DIR / 'test_catalog.db').as_posix()}"
os.environ["OUTPUT_DIR"] = str(_TEST_DIR / "output")
os.environ["JEV_API_KEY"] = ""
os.environ["JEV_MODE"] = "mock"

import pytest
from fastapi.testclient import TestClient

from app.db.database import Base, SessionLocal, engine, init_db
from app.main import app


@pytest.fixture()
def db_session():
    """Fresh tables for each test, dropped afterwards."""
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    """FastAPI test client bound to the throwaway database."""
    with TestClient(app) as test_client:
        yield test_client
