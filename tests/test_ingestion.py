"""
End-to-end ingestion tests (mock JEV, throwaway database): the sample catalog keeps its expected
outcome, other suppliers' headers are handled, and column-mapping problems send rows to review.
"""
from io import BytesIO
from typing import List

from openpyxl import Workbook

from app.core.config import settings
from app.models.catalog import Product, StagingRecord
from app.services.approval_service import ApprovalService
from app.services.ingestion_service import IngestionService

SAMPLE = str(settings.DATA_DIR / "sample_catalog.xlsx")


def workbook_bytes(rows: List[list]) -> bytes:
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def ingest(db_session, source, filename="supplier.xlsx"):
    summary = IngestionService().process_catalog(source=source, filename=filename, db=db_session)
    records = db_session.query(StagingRecord).order_by(StagingRecord.source_row_index).all()
    return summary, {record.source_row_index: record for record in records}


def test_sample_catalog_outcome_is_unchanged(db_session):
    summary, _ = ingest(db_session, str(settings.DATA_DIR / "sample_catalog.xlsx"), "sample_catalog.xlsx")
    assert (summary.ready_count, summary.needs_review_count, summary.rejected_count) == (7, 2, 4)
    assert summary.mapping_issues == []


def test_other_supplier_headers_and_formats(db_session):
    source = workbook_bytes([
        ["SKU", "Item Label", "Price", "Max Load", "Arm Length", "Curr.", "Warehouse Location"],
        ["B-1", "Gripper", "$100.00", "1,500 kg", "1,200 mm", "USD", "A1"],
        ["B-2", "Feeder", "CA$ 50", "5 tonnes", "2 m", None, "A2"],
        ["B-3", "Sensor", "€ 20", "N/A", "1 m", "USD", "A3"],
    ])
    summary, rows = ingest(db_session, source)

    assert summary.column_mappings["Item Label"] == "name"
    assert summary.column_mappings["Warehouse Location"] == "irrelevant"

    assert rows[2].status == "ready"
    assert rows[2].normalized_data["payload_kg"] == 1500.0
    assert rows[2].normalized_data["reach_m"] == 1.2

    assert rows[3].status == "ready"
    assert rows[3].normalized_data["currency"] == "CAD"
    assert rows[3].normalized_data["payload_kg"] == 5000.0

    assert rows[4].status == "needs_review"
    assert "Currency conflict" in rows[4].status_reason
    assert "Could not read payload value" in rows[4].status_reason


def test_ambiguous_column_sends_rows_to_review(db_session):
    source = workbook_bytes([
        ["SKU", "Item Label", "Price", "Currency", "Weight"],
        ["W-1", "Gantry", "$500.00", "USD", "40 kg"],
    ])
    summary, rows = ingest(db_session, source)
    assert summary.column_mappings["Weight"] == "unmapped"
    assert rows[2].status == "needs_review"
    assert "No confident column for 'payload_kg'" in rows[2].status_reason
    assert rows[2].normalized_data["payload_kg"] is None  # the uncertain column is never read


def test_reimporting_the_same_file_updates_rows_instead_of_duplicating(db_session):
    first, _ = ingest(db_session, SAMPLE)
    second, _ = ingest(db_session, SAMPLE)
    assert (first.new_rows, first.updated_rows) == (13, 0)
    assert (second.new_rows, second.updated_rows) == (0, 13)
    assert first.source_file_hash == second.source_file_hash
    assert db_session.query(StagingRecord).count() == 13


def test_approval_is_idempotent(db_session):
    ingest(db_session, SAMPLE)
    first = ApprovalService().approve_eligible_records(db_session)
    ingest(db_session, SAMPLE)
    second = ApprovalService().approve_eligible_records(db_session)
    assert first.newly_approved_count == 7
    assert (second.eligible_count, second.newly_approved_count) == (0, 0)
    assert db_session.query(Product).count() == 7


def test_review_and_rejected_rows_never_reach_products(db_session):
    ingest(db_session, SAMPLE)
    ApprovalService().approve_eligible_records(db_session)
    approved_rows = {p.source_row_index for p in db_session.query(Product)}
    assert approved_rows == {2, 3, 4, 5, 6, 12, 14}


def test_rejected_row_keeps_its_parsed_values_and_cell_locations(db_session):
    _, rows = ingest(db_session, SAMPLE)
    rejected = rows[8]  # RB-1006: blank name
    assert rejected.status == "rejected"
    assert rejected.normalized_data["sku"] == "RB-1006"
    assert rejected.cell_coordinates["Part Number"] == "A8"
    assert rejected.validation_results == ["Missing required product name"]


def test_conversion_notes_are_stored_separately(db_session):
    _, rows = ingest(db_session, SAMPLE)
    assert "Converted 50.0 lbs -> 22.68 kg" in rows[5].normalization_notes
    assert "notes" not in rows[5].normalized_data


def test_known_sku_with_different_values_goes_to_review(db_session):
    ingest(db_session, workbook_bytes([["SKU", "Item Label", "Price", "Currency"], ["P-1", "Arm", "$100.00", "USD"]]))
    ApprovalService().approve_eligible_records(db_session)

    ingest(db_session, workbook_bytes([["SKU", "Item Label", "Price", "Currency"], ["P-1", "Arm", "$120.00", "USD"]]), "price_update.xlsx")
    changed = db_session.query(StagingRecord).filter_by(source_filename="price_update.xlsx").one()
    assert changed.status == "needs_review"
    assert "already an approved product with different values" in changed.status_reason
    assert "unit_price" in changed.status_reason
    assert db_session.query(Product).one().unit_price == 100


def test_missing_name_column_is_a_review_not_a_rejection(db_session):
    source = workbook_bytes([
        ["SKU", "Price", "Currency"],
        ["N-1", "$10.00", "USD"],
    ])
    _, rows = ingest(db_session, source)
    assert rows[2].status == "needs_review"
    assert "No column in the file was identified as 'name'" in rows[2].status_reason
