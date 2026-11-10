from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.catalog import Product, StagingRecord
from .approval_service import ApprovalService
from .normalizer import NormalizedRowResult, Normalizer
from .validator import Validator

EDITABLE_FIELDS = ("sku", "name", "unit_price", "currency", "payload_kg", "reach_m")


class ResolutionError(Exception):
    """A review decision that cannot be applied; the message says why."""


class ReviewService:
    """
    Resolves 'needs_review' records. A reviewer can approve (optionally correcting values) or reject.
    Corrected values go through the same parsing and validation as ingestion: a reviewer can no more
    push an invalid value into products than JEV can.
    """

    def __init__(self):
        self.normalizer = Normalizer()

    def resolve(
        self,
        db: Session,
        staging_id: int,
        action: str,
        note: str,
        overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[StagingRecord, Optional[Product]]:
        record = db.get(StagingRecord, staging_id)
        if record is None:
            raise LookupError(f"Staging record {staging_id} not found")
        if record.status != "needs_review":
            raise ResolutionError(f"Only 'needs_review' records can be resolved; record {staging_id} is '{record.status}'")
        if not note or not note.strip():
            raise ResolutionError("A note explaining the decision is required")
        overrides = overrides or {}
        unknown = sorted(set(overrides) - set(EDITABLE_FIELDS))
        if unknown:
            raise ResolutionError(f"Unknown field(s) {unknown}; editable fields are {list(EDITABLE_FIELDS)}")

        resolution = {
            "action": action,
            "overrides": overrides,
            "note": note.strip(),
            "previous_status": record.status,
            "previous_reason": record.status_reason,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }

        if action == "reject":
            record.status = "rejected"
            record.status_reason = f"Rejected by reviewer: {note.strip()}"
            record.validation_results = [record.status_reason]
            record.review_resolution = resolution
            db.commit()
            return record, None

        if action != "approve":
            raise ResolutionError("action must be 'approve' or 'reject'")

        corrected = self._apply_overrides(record.normalized_data or {}, overrides)
        outcome = Validator().validate_and_classify(corrected, excel_row_index=record.source_row_index)
        if outcome.status != "ready":
            raise ResolutionError(f"Still not valid after review: {outcome.status_reason}")
        if db.query(Product).filter(Product.sku == corrected.sku).first():
            raise ResolutionError(f"SKU '{corrected.sku}' is already an approved product; it cannot be approved twice")

        record.normalized_data = outcome.normalized_dict
        record.status = "ready"
        record.status_reason = None
        record.validation_results = []
        record.review_resolution = resolution
        db.commit()

        ApprovalService().approve_eligible_records(db, staging_ids=[record.id])
        product = db.query(Product).filter(Product.staging_record_id == record.id).first()
        return record, product

    def _apply_overrides(self, data: Dict, overrides: Dict[str, str]) -> NormalizedRowResult:
        """Start from the staged values and re-parse each corrected field exactly as ingestion would."""
        result = NormalizedRowResult(
            sku=data.get("sku"),
            name=data.get("name"),
            unit_price=Decimal(data["unit_price"]) if data.get("unit_price") is not None else None,
            currency=data.get("currency"),
            payload_kg=data.get("payload_kg"),
            reach_m=data.get("reach_m"),
        )

        for field_name in ("sku", "name"):
            if field_name in overrides:
                setattr(result, field_name, overrides[field_name].strip() or None)

        if "unit_price" in overrides:
            price, _, error = self.normalizer.parse_price(overrides["unit_price"])
            if error:
                raise ResolutionError(f"unit_price: {error}")
            result.unit_price = price

        code, needs_review, reason = self.normalizer.resolve_currency(overrides.get("currency", result.currency), None)
        result.currency = code
        result.is_currency_ambiguous = needs_review
        result.currency_ambiguity_reason = reason

        # A bare number is read in the target unit ("12" -> 12 kg), via the header-unit rule
        for field_name, normalize, header in (
            ("payload_kg", self.normalizer.normalize_payload_kg, "payload (kg)"),
            ("reach_m", self.normalizer.normalize_reach_m, "reach (m)"),
        ):
            if field_name in overrides:
                value, _, issue = normalize(overrides[field_name], header=header)
                if issue:
                    raise ResolutionError(f"{field_name}: {issue}")
                setattr(result, field_name, value)
        return result
