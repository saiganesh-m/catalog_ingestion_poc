"""
End-to-End Demonstration Script for JEV Catalog Ingestion POC.
Executes and prints all 6 demonstration requirements specified in docs/original_brief.txt:
1. The original source.
2. The column mapping and JEV decision.
3. Normalized staged records.
4. Review/rejection reasons.
5. Approved database records.
6. A repeated import demonstrating duplicate protection (each claim is checked, not just printed).

Run from the project root:  python -m scripts.run_demo [path/to/catalog.xlsx]
Exit code 0 = every step-6 check passed, 1 = at least one failed.
"""
import sys
from tabulate import tabulate
from colorama import init, Fore, Style

# Initialize colorama for cross-platform terminal colors
init(autoreset=True)

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine, init_db
from app.models.catalog import StagingRecord, Product
from app.services.excel_parser import ExcelParser
from app.services.ingestion_service import IngestionService
from app.services.approval_service import ApprovalService

STATUS_COLORS = {"ready": Fore.GREEN, "needs_review": Fore.YELLOW, "rejected": Fore.RED}


def print_banner(title: str, step_num: int) -> None:
    print(f"\n{Fore.CYAN}{'='*80}")
    print(f"{Fore.YELLOW}STEP {step_num}: {title.upper()}")
    print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}")


def colored_status(status: str) -> str:
    return f"{STATUS_COLORS.get(status, '')}{status.upper()}{Style.RESET_ALL}"


def short(value, width: int = 24) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def run_full_demonstration(catalog_path: str = str(settings.DATA_DIR / "sample_catalog.xlsx")) -> bool:
    print(f"{Fore.GREEN}{Style.BRIGHT}================================================================================")
    print("           JEV CATALOG INGESTION POC - END-TO-END DEMONSTRATION")
    print(f"================================================================================{Style.RESET_ALL}")
    print(f"Database URL        : {settings.DATABASE_URL}")
    print(f"JEV Mode            : {settings.JEV_MODE} (live calls {'enabled' if settings.is_jev_live else 'disabled'})")
    print(f"Supported Currencies: {settings.SUPPORTED_CURRENCIES}")
    print(f"Target Catalog File : {catalog_path}")

    # Fresh database for the demonstration (recreates tables, so schema changes need no manual step)
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()

    # =========================================================================
    # DEMO 1: The Original Source
    # =========================================================================
    print_banner("1. The Original Source Workbook", 1)
    extraction = ExcelParser().parse(catalog_path)
    print(f"File: {extraction.filename} | Sheet: {extraction.sheet_name} | Total Rows: {len(extraction.rows)}\n")
    source_table = [
        [f"Row {r.row_index}"] + [short(r.data.get(header), 20) for header in extraction.headers]
        for r in extraction.rows
    ]
    print(tabulate(source_table, headers=["Excel Row"] + [short(h, 20) for h in extraction.headers], tablefmt="rounded_outline"))

    # =========================================================================
    # DEMO 2: Column Mapping & JEV Decision
    # =========================================================================
    print_banner("2. JEV Column Mapping & Probabilistic Decisions", 2)
    ingestion_service = IngestionService()
    jev_result = ingestion_service.jev_service.map_columns(extraction.headers, extraction.column_samples)

    print(f"Model Name     : {Fore.GREEN}{jev_result.model_name}{Style.RESET_ALL}")
    print(f"Execution Mode : {Fore.YELLOW}{jev_result.execution_mode}{Style.RESET_ALL}")
    for call in jev_result.calls:
        print(f"JEV Call       : {call['round']:9} {call['outcome']:7} request_id={call.get('request_id')}")
        if call.get("evidence_file"):
            print(f"  Evidence     : {call['evidence_file']}")
    if jev_result.blocker_message:
        print(f"Notice         : {Fore.MAGENTA}{jev_result.blocker_message}{Style.RESET_ALL}")

    verdict_colors = {"confirmed": Fore.GREEN, "confident": Fore.GREEN, "irrelevant": Fore.CYAN, "ruled_out": Fore.CYAN}
    jev_table = []
    for col, dec in jev_result.field_decisions.items():
        ranked = sorted(dec['probabilities'].items(), key=lambda item: item[1], reverse=True)
        probs_summary = ", ".join([f"{k}:{v:.2f}" for k, v in ranked[:3]])
        color = verdict_colors.get(dec['verdict'], Fore.YELLOW)
        notes = dec.get("note") or "Direct mapping"
        jev_table.append([
            col,
            dec['choice'] or "-",
            f"{dec['confidence']:.2f}",
            probs_summary,
            f"{dec['noul']:.2f}" if dec['noul'] is not None else "-",
            f"{color}{dec['verdict']}{Style.RESET_ALL}",
            f"{Fore.GREEN}{dec['target_field']}{Style.RESET_ALL}",
            notes[:45] + ("..." if len(notes) > 45 else ""),
        ])
    jev_headers = ["Source Column", "JEV Choice", "Conf.", "Top Probabilities", "Yes/No Check", "Verdict", "Mapped To", "Decision Note"]
    print("\n" + tabulate(jev_table, headers=jev_headers, tablefmt="rounded_outline"))

    field_table = [
        [target, status["status"], status["column"] or "-", status["reason"] or ""]
        for target, status in jev_result.field_status.items()
    ]
    print("\nField resolution (Python decides, per field):")
    print(tabulate(field_table, headers=["Target Field", "Status", "Column", "Review Reason"], tablefmt="rounded_outline"))

    # =========================================================================
    # DEMO 3: Normalized Staged Records
    # =========================================================================
    print_banner("3. Normalized Staged Records (Tri-State Status)", 3)
    summary = ingestion_service.process_catalog(catalog_path, extraction.filename, db=db, jev_result=jev_result)
    print(f"Ingestion Result: Total={summary.total_rows} (new={summary.new_rows}, updated={summary.updated_rows}) | "
          f"{Fore.GREEN}Ready={summary.ready_count}{Style.RESET_ALL} | "
          f"{Fore.YELLOW}Needs Review={summary.needs_review_count}{Style.RESET_ALL} | "
          f"{Fore.RED}Rejected={summary.rejected_count}{Style.RESET_ALL}\n")

    staged_records = db.query(StagingRecord).order_by(StagingRecord.source_row_index.asc()).all()
    staged_table = []
    for rec in staged_records:
        norm = rec.normalized_data or {}
        price = norm.get("unit_price")
        price_disp = f"{price} {norm.get('currency') or '(currency unresolved)'}" if price is not None else "-"
        staged_table.append([
            f"Row {rec.source_row_index}",
            norm.get("sku") or "-",
            short(norm.get("name") or "-", 28),
            price_disp,
            f"{norm['payload_kg']} kg" if norm.get("payload_kg") is not None else "-",
            f"{norm['reach_m']} m" if norm.get("reach_m") is not None else "-",
            short("; ".join(rec.normalization_notes or []), 34),
            colored_status(rec.status),
        ])
    staged_headers = ["Excel Row", "SKU", "Product Name", "Price & Currency", "Payload", "Reach", "Conversions", "Status"]
    print(tabulate(staged_table, headers=staged_headers, tablefmt="rounded_outline"))

    # =========================================================================
    # DEMO 4: Review and Rejection Reasons
    # =========================================================================
    print_banner("4. Audit Trail: Detailed Review & Rejection Reasons", 4)
    flagged_records = (
        db.query(StagingRecord)
        .filter(StagingRecord.status.in_(["needs_review", "rejected"]))
        .order_by(StagingRecord.status, StagingRecord.source_row_index)
        .all()
    )
    reasons_table = [
        [
            f"Row {rec.source_row_index}",
            (rec.normalized_data or {}).get("sku") or "<blank>",
            colored_status(rec.status),
            rec.status_reason or "N/A",
        ]
        for rec in flagged_records
    ]
    print(tabulate(reasons_table, headers=["Excel Row", "SKU", "Status", "Reason"], tablefmt="rounded_outline"))

    # =========================================================================
    # DEMO 5: Approved Database Records
    # =========================================================================
    print_banner("5. Approved Products in Database", 5)
    approval_service = ApprovalService()
    appr_summary = approval_service.approve_eligible_records(db=db)
    print(f"{Fore.GREEN}{appr_summary.message}{Style.RESET_ALL}\n")

    products = db.query(Product).order_by(Product.id.asc()).all()
    prod_table = [
        [
            p.id,
            p.sku,
            short(p.name, 35),
            f"{p.unit_price:.2f}",
            p.currency,
            f"{p.payload_kg:.3f}" if p.payload_kg is not None else "-",
            f"{p.reach_m:.3f}" if p.reach_m is not None else "-",
            f"{p.source_filename} (Row {p.source_row_index})",
        ]
        for p in products
    ]
    prod_headers = ["ID", "SKU", "Approved Product Name", "Unit Price", "Currency", "Payload (kg)", "Reach (m)", "Provenance Source"]
    print(tabulate(prod_table, headers=prod_headers, tablefmt="rounded_outline"))

    # =========================================================================
    # DEMO 6: Repeated Import Demonstrating Duplicate Protection (Idempotency)
    # =========================================================================
    print_banner("6. Repeated Ingestion & Idempotency Protection", 6)
    print("Re-importing the same workbook (fresh JEV mapping) and approving again...\n")
    staged_before = db.query(StagingRecord).count()
    products_before = len(products)

    second_summary = ingestion_service.process_catalog(catalog_path, extraction.filename, db=db)
    second_appr = approval_service.approve_eligible_records(db=db)
    staged_after = db.query(StagingRecord).count()
    products_after = db.query(Product).count()

    checks = [
        ("Re-import adds no new staging rows", second_summary.new_rows == 0, f"new={second_summary.new_rows}, updated={second_summary.updated_rows}"),
        ("Staging row count unchanged", staged_after == staged_before, f"{staged_before} -> {staged_after}"),
        ("Second approval creates no products", second_appr.newly_approved_count == 0, f"newly approved={second_appr.newly_approved_count}"),
        ("Product count unchanged", products_after == products_before, f"{products_before} -> {products_after}"),
        ("Same statuses on re-import",
         (second_summary.ready_count, second_summary.needs_review_count, second_summary.rejected_count)
         == (summary.ready_count, summary.needs_review_count, summary.rejected_count),
         f"{second_summary.ready_count}/{second_summary.needs_review_count}/{second_summary.rejected_count}"),
    ]
    check_table = [
        [name, f"{Fore.GREEN}PASS{Style.RESET_ALL}" if ok else f"{Fore.RED}FAIL{Style.RESET_ALL}", detail]
        for name, ok, detail in checks
    ]
    print(tabulate(check_table, headers=["Check", "Result", "Detail"], tablefmt="rounded_outline"))

    all_passed = all(ok for _, ok, _ in checks)
    if all_passed:
        print(f"\n{Fore.GREEN}{Style.BRIGHT}DEMONSTRATION COMPLETE - ALL IDEMPOTENCY CHECKS PASSED{Style.RESET_ALL}\n")
    else:
        print(f"\n{Fore.RED}{Style.BRIGHT}DEMONSTRATION FINISHED WITH FAILED CHECKS - SEE ABOVE{Style.RESET_ALL}\n")

    db.close()
    return all_passed


if __name__ == "__main__":
    passed = run_full_demonstration(sys.argv[1]) if len(sys.argv) > 1 else run_full_demonstration()
    sys.exit(0 if passed else 1)
