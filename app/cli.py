"""
Command-line interface for the catalog ingestion pipeline (the alternative to the FastAPI endpoints).

Run from the project root:
  python -m app.cli ingest data/sample_catalog.xlsx [--sheet NAME]
  python -m app.cli staging [--status needs_review] [--file sample_catalog.xlsx]
  python -m app.cli show 11
  python -m app.cli approve [--ids 1 2 3]
  python -m app.cli resolve 11 --approve --set currency=USD --note "Supplier confirmed USD"
  python -m app.cli resolve 13 --reject --note "Duplicate row"
  python -m app.cli products
  python -m app.cli reset-db --yes
"""
import argparse
import json
import sys
from typing import Dict, List

from tabulate import tabulate

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine, init_db
from app.models.catalog import Product, StagingRecord
from app.services.approval_service import ApprovalService
from app.services.ingestion_service import IngestionService
from app.services.jev import JevApiError
from app.services.review_service import ResolutionError, ReviewService


def cmd_ingest(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        summary = IngestionService().process_catalog(args.path, filename=args.path, db=db, sheet_name=args.sheet)
    print(f"File      : {summary.source_filename}  (sheet '{summary.source_sheet}', sha256 {summary.source_file_hash[:12]}…)")
    print(f"JEV       : {summary.jev_mode}  model={summary.jev_model}")
    if summary.jev_notice:
        print(f"Notice    : {summary.jev_notice}")
    for path in summary.jev_evidence_files:
        print(f"Evidence  : {path}")
    print(f"Rows      : {summary.total_rows} (new {summary.new_rows}, updated {summary.updated_rows})")
    print(f"Status    : ready {summary.ready_count} | needs_review {summary.needs_review_count} | rejected {summary.rejected_count}")
    print("\n" + tabulate(sorted(summary.column_mappings.items()), headers=["Column", "Mapped to"], tablefmt="simple"))
    for issue in summary.mapping_issues:
        print(f"Mapping issue: {issue}")
    return 0


def cmd_staging(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        query = db.query(StagingRecord)
        if args.status:
            query = query.filter(StagingRecord.status == args.status)
        if args.file:
            query = query.filter(StagingRecord.source_filename == args.file)
        records = query.order_by(StagingRecord.id).all()
        rows = [
            [r.id, r.source_filename, r.source_row_index, (r.normalized_data or {}).get("sku") or "-", r.status,
             (r.status_reason or "")[:90]]
            for r in records
        ]
    print(tabulate(rows, headers=["ID", "File", "Row", "SKU", "Status", "Reason"], tablefmt="simple"))
    print(f"\n{len(rows)} record(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        record = db.get(StagingRecord, args.id)
        if record is None:
            print(f"Staging record {args.id} not found", file=sys.stderr)
            return 1
        detail = {
            "id": record.id,
            "status": record.status,
            "status_reason": record.status_reason,
            "validation_results": record.validation_results,
            "source": {
                "filename": record.source_filename,
                "sha256": record.source_file_hash,
                "sheet": record.source_sheet,
                "row": record.source_row_index,
                "cells": record.cell_coordinates,
            },
            "raw_data": record.raw_data,
            "normalized_data": record.normalized_data,
            "normalization_notes": record.normalization_notes,
            "jev": {
                "execution_mode": record.jev_decisions.get("execution_mode"),
                "model": record.jev_decisions.get("model_name"),
                "mappings": record.jev_decisions.get("mappings"),
                "calls": record.jev_decisions.get("calls"),
            },
            "review_resolution": record.review_resolution,
        }
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        summary = ApprovalService().approve_eligible_records(db, staging_ids=args.ids)
    print(summary.message)
    if summary.approved_skus:
        print("Approved SKUs: " + ", ".join(summary.approved_skus))
    return 0


def parse_overrides(pairs: List[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for pair in pairs:
        field_name, separator, value = pair.partition("=")
        if not separator:
            raise ResolutionError(f"--set expects field=value, got '{pair}'")
        overrides[field_name.strip()] = value.strip()
    return overrides


def cmd_resolve(args: argparse.Namespace) -> int:
    action = "approve" if args.approve else "reject"
    with SessionLocal() as db:
        record, product = ReviewService().resolve(db, args.id, action, args.note, parse_overrides(args.set or []))
        if product:
            print(f"Record {record.id} approved -> product {product.sku}: {product.name}, "
                  f"{product.unit_price:.2f} {product.currency}")
        else:
            print(f"Record {record.id} rejected: {record.status_reason}")
    return 0


def cmd_products(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        rows = [
            [p.id, p.sku, p.name[:40], f"{p.unit_price:.2f}", p.currency,
             p.payload_kg if p.payload_kg is not None else "-", p.reach_m if p.reach_m is not None else "-",
             f"{p.source_filename} row {p.source_row_index}"]
            for p in db.query(Product).order_by(Product.id)
        ]
    print(tabulate(rows, headers=["ID", "SKU", "Name", "Price", "Cur", "Payload kg", "Reach m", "Source"], tablefmt="simple"))
    print(f"\n{len(rows)} product(s)")
    return 0


def cmd_reset_db(args: argparse.Namespace) -> int:
    if not args.yes:
        print("This deletes every staged record and product. Re-run with --yes to confirm.", file=sys.stderr)
        return 1
    Base.metadata.drop_all(bind=engine)
    init_db()
    print(f"Database reset: {settings.DATABASE_URL}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="JEV catalog ingestion CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Import an Excel catalog into staging")
    ingest.add_argument("path")
    ingest.add_argument("--sheet", help="Worksheet name (default: the active sheet)")
    ingest.set_defaults(handler=cmd_ingest)

    staging = commands.add_parser("staging", help="List staged records")
    staging.add_argument("--status", choices=["ready", "needs_review", "rejected"])
    staging.add_argument("--file", help="Filter by source filename")
    staging.set_defaults(handler=cmd_staging)

    show = commands.add_parser("show", help="Show the full audit trail of one staged record")
    show.add_argument("id", type=int)
    show.set_defaults(handler=cmd_show)

    approve = commands.add_parser("approve", help="Approve 'ready' records into products (idempotent)")
    approve.add_argument("--ids", type=int, nargs="+", help="Only these staging IDs")
    approve.set_defaults(handler=cmd_approve)

    resolve = commands.add_parser("resolve", help="Resolve a 'needs_review' record")
    resolve.add_argument("id", type=int)
    decision = resolve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true", help="Approve (after applying any --set corrections)")
    decision.add_argument("--reject", action="store_true", help="Reject")
    resolve.add_argument("--set", action="append", metavar="FIELD=VALUE",
                         help="Correct a value, e.g. --set currency=USD --set payload_kg=12kg")
    resolve.add_argument("--note", required=True, help="Why this decision was made")
    resolve.set_defaults(handler=cmd_resolve)

    products = commands.add_parser("products", help="List approved products")
    products.set_defaults(handler=cmd_products)

    reset = commands.add_parser("reset-db", help="Drop and recreate all tables")
    reset.add_argument("--yes", action="store_true", help="Confirm deleting all data")
    reset.set_defaults(handler=cmd_reset_db)
    return parser


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    init_db()
    try:
        return args.handler(args)
    except (ResolutionError, LookupError, ValueError, FileNotFoundError, JevApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
