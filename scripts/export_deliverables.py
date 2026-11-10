"""
Runs the full pipeline on both sample workbooks and writes the submission deliverables to output/:

  ingestion_summary.json         per-file summaries, approval results and the re-import (idempotency) check
  sample_normalized_output.json  every staged record with provenance, normalized values and reasons
  approved_products.json         the final products table
  jev_call_evidence.json         index of the live JEV exchanges saved in output/jev_evidence/
  EVALUATION.md                  mapping decisions, edge-case results (checked, not hand-written), values

Uses the configured JEV_MODE (live by default). Resets the local database first.
Run from the project root:  python -m scripts.export_deliverables
"""
import json
import sys
from decimal import Decimal
from typing import Any, Callable, Dict, List, Tuple

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine, init_db
from app.models.catalog import Product, StagingRecord
from app.services.approval_service import ApprovalService
from app.services.ingestion_service import IngestionService

SAMPLE = "sample_catalog.xlsx"
SUPPLIER_B = "sample_catalog_supplier_b.xlsx"


def to_json(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def write_json(name: str, data: Any) -> None:
    path = settings.OUTPUT_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=to_json), encoding="utf-8")
    print(f"Wrote {path}")


def export_deliverables() -> bool:
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    approvals = ApprovalService()

    # Main sample, approve; then supplier B (its RB-1003 row now meets an approved SKU), approve
    summaries = {SAMPLE: IngestionService().process_catalog(str(settings.DATA_DIR / SAMPLE), SAMPLE, db=db)}
    approval_runs = [approvals.approve_eligible_records(db)]
    summaries[SUPPLIER_B] = IngestionService().process_catalog(str(settings.DATA_DIR / SUPPLIER_B), SUPPLIER_B, db=db)
    approval_runs.append(approvals.approve_eligible_records(db))

    # Idempotency: re-import the main sample and approve again
    staged_before, products_before = db.query(StagingRecord).count(), db.query(Product).count()
    reimport = IngestionService().process_catalog(str(settings.DATA_DIR / SAMPLE), SAMPLE, db=db)
    reapproval = approvals.approve_eligible_records(db)
    idempotency = {
        "reimport_new_rows": reimport.new_rows,
        "reimport_updated_rows": reimport.updated_rows,
        "staging_rows_before_after": [staged_before, db.query(StagingRecord).count()],
        "products_before_after": [products_before, db.query(Product).count()],
        "second_approval_newly_approved": reapproval.newly_approved_count,
    }
    idempotency["passed"] = (
        reimport.new_rows == 0
        and idempotency["staging_rows_before_after"][0] == idempotency["staging_rows_before_after"][1]
        and idempotency["products_before_after"][0] == idempotency["products_before_after"][1]
        and reapproval.newly_approved_count == 0
    )

    records = db.query(StagingRecord).order_by(StagingRecord.source_filename, StagingRecord.source_row_index).all()
    products = db.query(Product).order_by(Product.id).all()

    write_json("ingestion_summary.json", {
        "files": [summary.model_dump() for summary in summaries.values()],
        "approvals": [run.model_dump() for run in approval_runs],
        "idempotency_check": idempotency,
    })
    write_json("sample_normalized_output.json", [
        {
            "staging_id": r.id,
            "source": {"filename": r.source_filename, "sha256": r.source_file_hash, "sheet": r.source_sheet,
                       "row": r.source_row_index, "cells": r.cell_coordinates},
            "status": r.status,
            "status_reason": r.status_reason,
            "validation_results": r.validation_results,
            "raw_data": r.raw_data,
            "normalized_data": r.normalized_data,
            "normalization_notes": r.normalization_notes,
            "jev": {"execution_mode": r.jev_decisions.get("execution_mode"), "model": r.jev_decisions.get("model_name"),
                    "mappings": r.jev_decisions.get("mappings")},
        }
        for r in records
    ])
    write_json("approved_products.json", [
        {"id": p.id, "sku": p.sku, "name": p.name, "unit_price": p.unit_price, "currency": p.currency,
         "payload_kg": p.payload_kg, "reach_m": p.reach_m,
         "source": f"{p.source_filename} row {p.source_row_index}", "staging_record_id": p.staging_record_id}
        for p in products
    ])

    jev_runs = {name: next(r.jev_decisions for r in records if r.source_filename == name) for name in summaries}
    evidence_files = sorted((settings.OUTPUT_DIR / "jev_evidence").glob("jev_call_*.json"))
    write_json("jev_call_evidence.json", {
        "note": ("Files in output/jev_evidence/ are real request/response exchanges with the JEV API, saved "
                 "automatically with the API key redacted. Mock answers are never saved there."),
        "this_run": {
            name: {"execution_mode": run["execution_mode"], "is_live": run["is_live"], "model": run["model_name"],
                   "blocker": run["blocker_message"], "calls": run["calls"]}
            for name, run in jev_runs.items()
        },
        "successful_live_calls_on_record": [
            path.name for path in evidence_files
            if json.loads(path.read_text(encoding="utf-8")).get("outcome") == "success"
        ],
    })

    checks = edge_case_checks(summaries, records, products)
    report = evaluation_report(summaries, jev_runs, records, products, checks, idempotency)
    (settings.OUTPUT_DIR / "EVALUATION.md").write_text(report, encoding="utf-8")
    print(f"Wrote {settings.OUTPUT_DIR / 'EVALUATION.md'}")

    db.close()
    passed = all(ok for _, _, ok in checks) and idempotency["passed"]
    print(f"\nEdge-case checks passed: {sum(ok for _, _, ok in checks)}/{len(checks)}; idempotency: "
          f"{'PASS' if idempotency['passed'] else 'FAIL'}")
    return passed


def edge_case_checks(summaries, records, products) -> List[Tuple[str, str, bool]]:
    """(scenario, what is checked, passed). Each check reads the real staged/approved data."""
    rows = {(r.source_filename, r.source_row_index): r for r in records}

    def row(name: str, number: int) -> StagingRecord:
        return rows[(name, number)]

    def norm(name: str, number: int, field: str) -> Any:
        return (row(name, number).normalized_data or {}).get(field)

    def reason(name: str, number: int) -> str:
        return row(name, number).status_reason or ""

    maps_a, maps_b = summaries[SAMPLE].column_mappings, summaries[SUPPLIER_B].column_mappings
    cases: List[Tuple[str, str, Callable[[], bool]]] = [
        ("1. Header synonyms", "'Part Number' and 'Product Code' both -> sku",
         lambda: maps_a.get("Part Number") == "sku" and maps_b.get("Product Code") == "sku"),
        ("2. Product name variations", "'Product Description' and 'Item Label' both -> name",
         lambda: maps_a.get("Product Description") == "name" and maps_b.get("Item Label") == "name"),
        ("3. Formatted prices", "'€ 2.450,50' -> 2450.50 EUR; '₹ 1,85,000.00' -> 185000.00; '1.250,00 €' -> 1250.00",
         lambda: norm(SAMPLE, 3, "unit_price") == "2450.50" and norm(SAMPLE, 3, "currency") == "EUR"
         and norm(SAMPLE, 6, "unit_price") == "185000.00" and norm(SUPPLIER_B, 4, "unit_price") == "1250.00"),
        ("4. Embedded units", "'50 lbs' -> 22.68 kg, '78 in' -> 1.981 m, '1,500 kg' -> 1500, '0,5 t' -> 500 kg",
         lambda: norm(SAMPLE, 5, "payload_kg") == 22.68 and norm(SAMPLE, 5, "reach_m") == 1.981
         and norm(SUPPLIER_B, 2, "payload_kg") == 1500.0 and norm(SUPPLIER_B, 4, "payload_kg") == 500.0),
        ("5. Missing required fields", "blank SKU (row 7) and blank name (row 8) -> rejected",
         lambda: row(SAMPLE, 7).status == "rejected" and row(SAMPLE, 8).status == "rejected"),
        ("6. Invalid numeric values", "'N/A' and '-$450.00' -> rejected; '-3 kg' payload -> needs_review",
         lambda: row(SAMPLE, 9).status == "rejected" and row(SAMPLE, 10).status == "rejected"
         and row(SUPPLIER_B, 8).status == "needs_review"),
        ("7. Duplicate products", "RB-1001 repeated in file -> needs_review; RB-1003 at a new price -> needs_review",
         lambda: "Duplicate SKU" in reason(SAMPLE, 13) and "already an approved product" in reason(SUPPLIER_B, 10)),
        ("8. Irrelevant columns", "'Warranty Duration' and 'Warehouse Location' -> irrelevant",
         lambda: maps_a.get("Warranty Duration") == "irrelevant" and maps_b.get("Warehouse Location") == "irrelevant"),
        ("Competing price columns", "'Unit Cost' over 'Extended Amount'; 'Price' over 'Line Total'",
         lambda: maps_a.get("Unit Cost") == "unit_price" and maps_a.get("Extended Amount") != "unit_price"
         and maps_b.get("Price") == "unit_price" and maps_b.get("Line Total") != "unit_price"),
        ("Rated Load / Weight vs payload", "'Rated Load' and 'Max Load' -> payload_kg; 'Weight' not payload",
         lambda: maps_a.get("Rated Load") == "payload_kg" and maps_b.get("Max Load") == "payload_kg"
         and maps_b.get("Weight") != "payload_kg"),
        ("Ambiguous currency", "bare '$' and '¥' -> needs_review, never guessed",
         lambda: "Ambiguous currency symbol '$'" in reason(SAMPLE, 11) and "Ambiguous currency symbol '¥'" in reason(SUPPLIER_B, 6)),
        ("Currency conflict", "'€ 3,900.00' with currency column USD -> needs_review",
         lambda: "Currency conflict" in reason(SUPPLIER_B, 5)),
        ("Missing optional values stay NULL", "RB-1012 payload/reach -> null, still ready",
         lambda: row(SAMPLE, 14).status == "ready" and norm(SAMPLE, 14, "payload_kg") is None),
        ("Review/rejected never approved", "every product comes from a 'ready' staged row",
         lambda: all(p.staging_record is not None and p.staging_record.status == "ready" for p in products)),
        ("Every flagged row has a reason", "all needs_review/rejected rows have status_reason",
         lambda: all(r.status_reason for r in records if r.status != "ready")),
    ]
    results = []
    for scenario, description, check in cases:
        try:
            ok = bool(check())
        except (KeyError, StopIteration):
            ok = False
        results.append((scenario, description, ok))
    return results


def evaluation_report(summaries, jev_runs, records, products, checks, idempotency) -> str:
    lines = [
        "# Evaluation Report",
        "",
        "Generated by `python -m scripts.export_deliverables` from a real pipeline run; nothing here is hand-written.",
        "",
    ]
    for name, run in jev_runs.items():
        live = "LIVE JEV" if run["is_live"] else "MOCK (not a real JEV call)"
        lines += [f"## Column mapping: `{name}` ({live}, model `{run['model_name']}`)", ""]
        if run["blocker_message"]:
            lines += [f"> Blocker: {run['blocker_message']}", ""]
        lines += ["| Column | JEV choice | Confidence | Yes/no check | Verdict | Mapped to |", "|---|---|---|---|---|---|"]
        for header, d in run["field_decisions"].items():
            check = f"{d['noul']:.2f}" if d["noul"] is not None else "-"
            lines.append(f"| {header} | {d['choice']} | {d['confidence']:.2f} | {check} | {d['verdict']} | {d['target_field']} |")
        lines += ["", "| Field | Status | Column | Reason |", "|---|---|---|---|"]
        for field_name, status in run["field_status"].items():
            lines.append(f"| {field_name} | {status['status']} | {status['column'] or '-'} | {status['reason'] or ''} |")
        lines.append("")

    lines += ["## Edge cases", "", "| Scenario | Checked | Result |", "|---|---|---|"]
    for scenario, description, ok in checks:
        lines.append(f"| {scenario} | {description} | {'PASS' if ok else 'FAIL'} |")

    lines += ["", "## Staged rows", "", "| File | Row | SKU | Price | Currency | Payload kg | Reach m | Status | Reason |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        n = r.normalized_data or {}
        reason = (r.status_reason or "").replace("|", "/")
        lines.append(f"| {r.source_filename} | {r.source_row_index} | {n.get('sku') or '-'} | {n.get('unit_price') or '-'} | "
                     f"{n.get('currency') or '-'} | {n.get('payload_kg') if n.get('payload_kg') is not None else '-'} | "
                     f"{n.get('reach_m') if n.get('reach_m') is not None else '-'} | {r.status} | {reason} |")

    lines += ["", f"## Approved products ({len(products)})", "", "| SKU | Name | Price | Currency | Source |", "|---|---|---|---|---|"]
    for p in products:
        lines.append(f"| {p.sku} | {p.name} | {p.unit_price:.2f} | {p.currency} | {p.source_filename} row {p.source_row_index} |")

    lines += ["", "## Idempotency (re-import of the main sample + second approval)", ""]
    lines += [f"- {key}: `{value}`" for key, value in idempotency.items()]
    for name, summary in summaries.items():
        lines.append(f"- `{name}`: ready {summary.ready_count}, needs_review {summary.needs_review_count}, rejected {summary.rejected_count}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(0 if export_deliverables() else 1)
