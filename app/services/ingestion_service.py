import hashlib
from decimal import Decimal
from io import BytesIO
from typing import Union, Optional, Dict, Any, List
from pathlib import Path
from sqlalchemy.orm import Session

from app.models.catalog import StagingRecord, Product
from app.schemas.catalog import IngestionSummary
from .excel_parser import ExcelParser
from .jev import JevService, JevMappingResult
from .normalizer import Normalizer
from .validator import Validator, ValidationOutcome

COMPARED_PRODUCT_FIELDS = ("name", "unit_price", "currency", "payload_kg", "reach_m")


class IngestionService:
    """
    End-to-end catalog ingestion orchestrator:
    1. Reads Excel cells & row coordinates (ExcelParser).
    2. Maps headers & disambiguates columns via typed questions (JevService).
    3. Normalizes prices, units, and currencies (Normalizer).
    4. Validates data integrity & classifies tri-state status (Validator).
    5. Persists complete audit provenance into staging_records table.

    Idempotency: a workbook is identified by the SHA-256 of its bytes. Re-importing the same file
    updates its staged rows in place (one row per file + sheet + Excel row) instead of adding duplicates.
    """

    def __init__(self):
        self.excel_parser = ExcelParser()
        self.jev_service = JevService()
        self.normalizer = Normalizer()
        self.validator = Validator()

    def process_catalog(
        self,
        source: Union[str, Path, bytes, BytesIO],
        filename: str,
        db: Session,
        sheet_name: Optional[str] = None,
        jev_result: Optional[JevMappingResult] = None,
    ) -> IngestionSummary:
        """
        Processes an incoming Excel workbook and records all rows in the staging table.
        Pass `jev_result` to reuse a mapping already obtained for this sheet instead of calling JEV again.
        """
        # Step 1: Programmatic Excel parsing
        content = self._read_bytes(source)
        file_hash = hashlib.sha256(content).hexdigest()
        if isinstance(source, (str, Path)):
            filename = Path(source).name
        extraction = self.excel_parser.parse(source=content, filename=filename, sheet_name=sheet_name)

        # Step 2: Intelligent column mapping via JEV (one mapping per ingestion)
        if jev_result is None:
            jev_result = self.jev_service.map_columns(
                headers=extraction.headers,
                column_samples=extraction.column_samples,
            )
        audit_jev_decisions = {
            "execution_mode": jev_result.execution_mode,
            "is_live": jev_result.is_live,
            "model_name": jev_result.model_name,
            "calls": jev_result.calls,
            "overall_confidence": jev_result.overall_confidence,
            "mappings": jev_result.column_mappings,
            "field_status": jev_result.field_status,
            "field_decisions": jev_result.field_decisions,
            "mapping_issues": jev_result.mapping_issues,
            "blocker_message": jev_result.blocker_message,
            "raw_responses": jev_result.raw_api_responses,
        }

        # Rows already staged from this exact file and sheet are updated, never duplicated
        existing: Dict[int, StagingRecord] = {
            record.source_row_index: record
            for record in db.query(StagingRecord).filter(
                StagingRecord.source_file_hash == file_hash,
                StagingRecord.source_sheet == extraction.sheet_name,
            )
        }

        # Step 3 & 4: Normalize, Validate & Stage each row
        self.validator.reset_file_state()
        counts = {"ready": 0, "needs_review": 0, "rejected": 0}
        new_rows = 0

        for raw_row in extraction.rows:
            record = existing.get(raw_row.row_index)
            if record is not None and record.review_resolution:
                # A reviewer already decided this row; re-importing the same file never undoes that
                counts[record.status] += 1
                continue

            norm_result = self.normalizer.normalize_row(
                raw_row_data=raw_row.data,
                column_mappings=jev_result.column_mappings,
            )
            outcome = self.validator.validate_and_classify(
                norm=norm_result,
                excel_row_index=raw_row.row_index,
                mapping_issues=jev_result.mapping_issues,
                unresolved_required_fields=jev_result.unresolved_required_fields,
            )
            self._check_against_approved_products(db, outcome)
            counts[outcome.status] += 1

            # Step 5: Save (or update) the record in the Staging Area
            if record is None:
                record = StagingRecord(
                    source_file_hash=file_hash,
                    source_sheet=raw_row.sheet_name,
                    source_row_index=raw_row.row_index,
                )
                db.add(record)
                new_rows += 1
            record.source_filename = extraction.filename
            record.cell_coordinates = raw_row.cell_coordinates
            record.raw_data = raw_row.data
            record.normalized_data = outcome.normalized_dict
            record.normalization_notes = norm_result.normalization_notes
            record.jev_decisions = audit_jev_decisions
            record.status = outcome.status
            record.status_reason = outcome.status_reason
            record.validation_results = outcome.reasons

        db.commit()

        return IngestionSummary(
            source_filename=extraction.filename,
            source_file_hash=file_hash,
            source_sheet=extraction.sheet_name,
            total_rows=len(extraction.rows),
            new_rows=new_rows,
            updated_rows=len(extraction.rows) - new_rows,
            ready_count=counts["ready"],
            needs_review_count=counts["needs_review"],
            rejected_count=counts["rejected"],
            column_mappings=jev_result.column_mappings,
            jev_mode=jev_result.execution_mode,
            jev_model=jev_result.model_name,
            jev_notice=jev_result.blocker_message,
            jev_evidence_files=jev_result.evidence_files,
            mapping_issues=jev_result.mapping_issues,
        )

    @staticmethod
    def _read_bytes(source: Union[str, Path, bytes, BytesIO]) -> bytes:
        if isinstance(source, bytes):
            return source
        if isinstance(source, BytesIO):
            return source.getvalue()
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Excel file not found at: {path}")
        return path.read_bytes()

    @staticmethod
    def _check_against_approved_products(db: Session, outcome: ValidationOutcome) -> None:
        """
        A SKU that is already an approved product must not silently change: identical values stay
        'ready' (approval will skip it), different values go to review with the differences listed.
        """
        if outcome.status == "rejected" or not outcome.normalized_dict:
            return
        sku = outcome.normalized_dict.get("sku")
        product = db.query(Product).filter(Product.sku == sku).first() if sku else None
        if product is None:
            return

        differences: List[str] = []
        for field_name in COMPARED_PRODUCT_FIELDS:
            approved: Any = getattr(product, field_name)
            incoming: Any = outcome.normalized_dict.get(field_name)
            if field_name == "unit_price":
                same = incoming is not None and Decimal(str(approved)) == Decimal(str(incoming))
            else:
                same = approved == incoming
            if not same:
                differences.append(f"{field_name} {approved} -> {incoming}")

        if differences:
            reason = f"SKU '{sku}' is already an approved product with different values: {', '.join(differences)}"
            outcome.status = "needs_review"
            outcome.reasons = [*outcome.reasons, reason]
            outcome.status_reason = " | ".join(outcome.reasons)
