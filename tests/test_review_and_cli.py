"""
Review resolution, CLI and API tests (mock JEV, throwaway database), plus the Supplier B sample
workbook built from the generator's own rows so the test never depends on files on disk.
"""
import pytest

from app.cli import main as cli_main
from app.core.config import settings
from app.models.catalog import Product, StagingRecord
from app.services.approval_service import ApprovalService
from app.services.ingestion_service import IngestionService
from app.services.review_service import ResolutionError, ReviewService
from scripts.generate_sample_catalog import SUPPLIER_B_HEADERS, SUPPLIER_B_ROWS, write_workbook

SAMPLE = str(settings.DATA_DIR / "sample_catalog.xlsx")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def staged(db_session, row, filename="sample_catalog.xlsx"):
    return db_session.query(StagingRecord).filter_by(source_filename=filename, source_row_index=row).one()


@pytest.fixture()
def sample_ingested(db_session):
    IngestionService().process_catalog(SAMPLE, "sample_catalog.xlsx", db=db_session)
    return db_session


# --- Review resolution -----------------------------------------------------------------------------

def test_reviewer_resolves_ambiguous_currency_and_the_row_is_approved(sample_ingested):
    db = sample_ingested
    record = staged(db, 11)  # RB-1009, bare '$'
    record, product = ReviewService().resolve(db, record.id, "approve", "Supplier confirmed USD", {"currency": "USD"})
    assert product.sku == "RB-1009" and product.currency == "USD"
    assert record.status == "ready"
    assert record.review_resolution["previous_status"] == "needs_review"
    assert record.review_resolution["overrides"] == {"currency": "USD"}


def test_reimport_never_undoes_a_reviewer_decision(sample_ingested):
    db = sample_ingested
    ReviewService().resolve(db, staged(db, 11).id, "approve", "Supplier confirmed USD", {"currency": "USD"})
    ReviewService().resolve(db, staged(db, 13).id, "reject", "Duplicate row")

    summary = IngestionService().process_catalog(SAMPLE, "sample_catalog.xlsx", db=db)
    db.expire_all()
    assert staged(db, 11).status == "ready"
    assert staged(db, 11).normalized_data["currency"] == "USD"
    assert staged(db, 13).status == "rejected"
    assert (summary.ready_count, summary.needs_review_count, summary.rejected_count) == (8, 0, 5)


def test_approval_without_a_fix_is_refused(sample_ingested):
    record = staged(sample_ingested, 11)
    with pytest.raises(ResolutionError, match="Still not valid"):
        ReviewService().resolve(sample_ingested, record.id, "approve", "looks fine")
    assert sample_ingested.query(Product).count() == 0


@pytest.mark.parametrize("overrides, message", [
    ({"currency": "USD", "unit_price": "abc"}, "unit_price"),
    ({"currency": "USD", "payload_kg": "-2 kg"}, "payload_kg must be greater than 0"),
    ({"currency": "XYZ"}, "not in supported list"),
    ({"colour": "red"}, "Unknown field"),
])
def test_reviewer_corrections_are_validated_like_ingestion(sample_ingested, overrides, message):
    record = staged(sample_ingested, 11)
    with pytest.raises(ResolutionError, match=message):
        ReviewService().resolve(sample_ingested, record.id, "approve", "fix", overrides)


def test_duplicate_of_an_approved_sku_cannot_be_approved(sample_ingested):
    ApprovalService().approve_eligible_records(sample_ingested)
    record = staged(sample_ingested, 13)  # duplicate RB-1001
    with pytest.raises(ResolutionError, match="already an approved product"):
        ReviewService().resolve(sample_ingested, record.id, "approve", "keep")


def test_reject_keeps_the_row_out_of_products(sample_ingested):
    record = staged(sample_ingested, 13)
    record, product = ReviewService().resolve(sample_ingested, record.id, "reject", "Duplicate row")
    assert product is None
    assert record.status == "rejected"
    assert record.status_reason == "Rejected by reviewer: Duplicate row"


def test_only_review_records_can_be_resolved(sample_ingested):
    record = staged(sample_ingested, 2)  # ready
    with pytest.raises(ResolutionError, match="Only 'needs_review'"):
        ReviewService().resolve(sample_ingested, record.id, "reject", "no")


# --- API -------------------------------------------------------------------------------------------

def test_api_upload_resolve_and_approve(client, db_session):
    with open(SAMPLE, "rb") as handle:
        response = client.post("/api/v1/ingest/upload", files={"file": ("sample_catalog.xlsx", handle.read(), XLSX)})
    assert response.status_code == 201
    assert response.json()["ready_count"] == 7

    record_id = staged(db_session, 11).id
    response = client.post(f"/api/v1/staging/{record_id}/resolve",
                           json={"action": "approve", "overrides": {"currency": "USD"}, "note": "confirmed"})
    assert response.status_code == 200
    assert response.json()["product"]["sku"] == "RB-1009"

    assert client.post(f"/api/v1/staging/{record_id}/resolve", json={"action": "reject", "note": "x"}).status_code == 409
    assert client.post("/api/v1/staging/99999/resolve", json={"action": "reject", "note": "x"}).status_code == 404

    response = client.post("/api/v1/products/approve")
    assert response.json()["newly_approved_count"] == 7
    assert len(client.get("/api/v1/products").json()) == 8


@pytest.mark.parametrize("filename, content, query", [
    ("broken.xlsx", b"not an excel file", ""),
    ("sample_catalog.xlsx", None, "?sheet=NoSuchSheet"),
])
def test_api_rejects_unreadable_input_with_400(client, filename, content, query):
    if content is None:
        with open(SAMPLE, "rb") as handle:
            content = handle.read()
    response = client.post(f"/api/v1/ingest/upload{query}", files={"file": (filename, content, XLSX)})
    assert response.status_code == 400


# --- CLI -------------------------------------------------------------------------------------------

def test_cli_end_to_end(db_session, capsys):
    assert cli_main(["ingest", SAMPLE]) == 0
    assert "ready 7 | needs_review 2 | rejected 4" in capsys.readouterr().out

    assert cli_main(["staging", "--status", "needs_review"]) == 0
    assert "RB-1009" in capsys.readouterr().out

    record_id = staged(db_session, 11).id
    assert cli_main(["resolve", str(record_id), "--approve", "--set", "currency=USD", "--note", "confirmed"]) == 0
    assert "approved -> product RB-1009" in capsys.readouterr().out

    assert cli_main(["approve"]) == 0
    assert "Approved 7 new product(s)" in capsys.readouterr().out

    assert cli_main(["products"]) == 0
    assert "8 product(s)" in capsys.readouterr().out

    assert cli_main(["show", str(record_id)]) == 0
    assert '"previous_status": "needs_review"' in capsys.readouterr().out


def test_cli_reports_errors_without_a_traceback(db_session, capsys):
    assert cli_main(["resolve", "99999", "--reject", "--note", "x"]) == 1
    assert "not found" in capsys.readouterr().err
    assert cli_main(["reset-db"]) == 1  # refuses without --yes


# --- Supplier B sample workbook --------------------------------------------------------------------

def test_supplier_b_workbook(db_session, tmp_path):
    path = tmp_path / "sample_catalog_supplier_b.xlsx"
    write_workbook(path, "Supplier_B_Price_List", SUPPLIER_B_HEADERS, SUPPLIER_B_ROWS)
    summary = IngestionService().process_catalog(str(path), path.name, db=db_session)

    assert summary.column_mappings["Product Code"] == "sku"
    assert summary.column_mappings["Price"] == "unit_price"
    assert summary.column_mappings["Line Total"] == "irrelevant"
    assert summary.column_mappings["Max Load"] == "payload_kg"
    assert summary.column_mappings["Weight"] != "payload_kg"

    status = {row: staged(db_session, row, path.name).status for row in range(2, 11)}
    assert status == {
        2: "ready", 3: "ready", 4: "ready", 5: "needs_review", 6: "needs_review",
        7: "rejected", 8: "needs_review", 9: "rejected", 10: "ready",
    }
    assert staged(db_session, 2, path.name).normalized_data["payload_kg"] == 1500.0
    assert staged(db_session, 4, path.name).normalized_data == {
        "sku": "SB-2003", "name": "Gantry Loader", "unit_price": "1250.00", "currency": "EUR",
        "payload_kg": 500.0, "reach_m": 1.219,
    }


def test_supplier_b_price_change_for_an_approved_sku_goes_to_review(sample_ingested, tmp_path):
    ApprovalService().approve_eligible_records(sample_ingested)
    path = tmp_path / "sample_catalog_supplier_b.xlsx"
    write_workbook(path, "Supplier_B_Price_List", SUPPLIER_B_HEADERS, SUPPLIER_B_ROWS)
    IngestionService().process_catalog(str(path), path.name, db=sample_ingested)

    record = staged(sample_ingested, 10, path.name)  # RB-1003 at £900 instead of £850
    assert record.status == "needs_review"
    assert "unit_price 850" in record.status_reason
