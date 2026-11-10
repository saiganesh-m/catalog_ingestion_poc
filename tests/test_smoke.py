"""Phase 0 smoke tests: the project layout, configuration and API start up correctly."""
from sqlalchemy import inspect

from app.core.config import BASE_DIR, settings
from app.db.database import engine


def test_tests_use_throwaway_database():
    assert "jev_tests_" in settings.DATABASE_URL
    assert settings.DATABASE_URL.endswith("/test_catalog.db")


def test_tests_run_in_mock_mode():
    assert settings.is_jev_live is False


def test_project_paths_resolve_from_root():
    assert (BASE_DIR / "app" / "main.py").is_file()
    assert (settings.DATA_DIR / "sample_catalog.xlsx").is_file()


def test_init_db_creates_both_tables(db_session):
    tables = set(inspect(engine).get_table_names())
    assert {"staging_records", "products"} <= tables


def test_inspect_db_runs_read_only_queries(db_session, capsys):
    from scripts import inspect_db

    assert inspect_db.run_query("SELECT COUNT(*) AS products FROM products") == 0
    assert "1 row(s)" in capsys.readouterr().out
    assert inspect_db.run_query("DELETE FROM products") == 1  # writes are refused


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["documentation"] == "/docs"


def test_health_endpoint_reports_mock_mode(client):
    response = client.get("/api/v1/ingest/health")
    assert response.status_code == 200
    body = response.json()
    assert body["jev_mode"] == "mock"
    assert body["jev_live_calls_enabled"] is False
    assert body["jev_endpoint"].endswith("/systemone")
