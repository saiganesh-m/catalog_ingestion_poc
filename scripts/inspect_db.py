"""
Look inside the database the app is using (SQLite, PostgreSQL or MySQL - whatever DATABASE_URL points to).

Run from the project root:
  python -m scripts.inspect_db                       overview: tables, row counts, status breakdown
  python -m scripts.inspect_db --schema              every table's columns and types
  python -m scripts.inspect_db --sql "SELECT sku, name, unit_price, currency FROM products"

--sql accepts read-only statements only (SELECT / WITH / PRAGMA / SHOW / DESCRIBE / EXPLAIN).
"""
import argparse
import sys

from sqlalchemy import inspect, text
from tabulate import tabulate

from app.core.config import settings
from app.db.database import engine

READ_ONLY_PREFIXES = ("select", "with", "pragma", "show", "describe", "explain")
MAX_CELL = 60


def shorten(value) -> str:
    text_value = "" if value is None else str(value)
    return text_value if len(text_value) <= MAX_CELL else text_value[: MAX_CELL - 1] + "…"


def overview() -> None:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Database: {settings.DATABASE_URL}\n")
    if not tables:
        print("No tables yet. Run any command (e.g. python -m app.cli staging) to create them.")
        return
    with engine.connect() as connection:
        counts = [[table, connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()] for table in tables]
        print(tabulate(counts, headers=["Table", "Rows"], tablefmt="simple"))
        if "staging_records" in tables:
            breakdown = connection.execute(text(
                "SELECT source_filename, status, COUNT(*) FROM staging_records "
                "GROUP BY source_filename, status ORDER BY source_filename, status"
            )).all()
            print("\nStaging records by file and status:")
            print(tabulate(breakdown, headers=["File", "Status", "Rows"], tablefmt="simple"))


def schema() -> None:
    inspector = inspect(engine)
    for table in inspector.get_table_names():
        columns = [[c["name"], str(c["type"]), "yes" if c["nullable"] else "no"] for c in inspector.get_columns(table)]
        print(f"\n{table}")
        print(tabulate(columns, headers=["Column", "Type", "Nullable"], tablefmt="simple"))
        for constraint in inspector.get_unique_constraints(table):
            print(f"  unique: {constraint['column_names']}")


def run_query(sql: str) -> int:
    if not sql.strip().lower().startswith(READ_ONLY_PREFIXES):
        print("Only read-only statements are allowed (SELECT, WITH, PRAGMA, SHOW, DESCRIBE, EXPLAIN).", file=sys.stderr)
        return 1
    with engine.connect() as connection:
        result = connection.execute(text(sql))
        rows = [[shorten(value) for value in row] for row in result]
        print(tabulate(rows, headers=list(result.keys()), tablefmt="simple"))
    print(f"\n{len(rows)} row(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.inspect_db", description="Inspect the app database")
    parser.add_argument("--schema", action="store_true", help="Show every table's columns and types")
    parser.add_argument("--sql", help="Run a read-only SQL query")
    args = parser.parse_args()
    if args.sql:
        return run_query(args.sql)
    if args.schema:
        schema()
    else:
        overview()
    return 0


if __name__ == "__main__":
    sys.exit(main())
