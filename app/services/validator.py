from decimal import Decimal
from typing import Optional, List, Dict, Any, Sequence
from dataclasses import dataclass, field
from app.core.config import settings
from .normalizer import NormalizedRowResult


@dataclass
class ValidationOutcome:
    """Represents the tri-state classification outcome for a processed catalog row."""
    status: str                         # 'ready', 'needs_review', 'rejected'
    status_reason: Optional[str]        # Detailed explanation for review or rejection
    normalized_dict: Optional[Dict[str, Any]]  # Normalized values (kept for rejected rows too, for the audit)
    reasons: List[str] = field(default_factory=list)  # Each individual review/rejection reason


class Validator:
    """
    Quality gatekeeper and tri-state staging classifier.
    Enforces the core rule:
    "A JEV confidence value must not override a failed numeric or required-field validation."
    """

    def __init__(self):
        self.seen_skus: Dict[str, int] = {}  # sku -> excel_row_index (for intra-file duplicate detection)

    def reset_file_state(self) -> None:
        """Reset internal duplicate tracking cache before processing a new file."""
        self.seen_skus.clear()

    def validate_and_classify(
        self,
        norm: NormalizedRowResult,
        excel_row_index: int,
        mapping_issues: Sequence[str] = (),
        unresolved_required_fields: Sequence[str] = (),
    ) -> ValidationOutcome:
        """
        Evaluates normalized candidate data and assigns tri-state status:
        - 'rejected': Fails hard rules (missing required value in a mapped column, negative price, corrupt numeric).
        - 'needs_review': Unresolved column mapping, ambiguous or conflicting currency, duplicate SKU,
                          or an unreadable / out-of-range optional value (payload, reach).
        - 'ready': All checks passed and every needed column was mapped with confidence.

        `mapping_issues` and `unresolved_required_fields` come from the JEV column mapping and apply to
        every row of the sheet. A required field with no trustworthy column is a mapping problem, so its
        empty value sends the row to review instead of rejecting it.
        """
        hard_rejection_reasons: List[str] = []
        review_reasons: List[str] = []

        # =========================================================================
        # 1. HARD VALIDATION CHECKS (Produces 'rejected')
        # =========================================================================

        # Check required SKU
        if "sku" not in unresolved_required_fields and (not norm.sku or not norm.sku.strip()):
            hard_rejection_reasons.append("Missing required product identifier: sku")

        # Check required Name
        if "name" not in unresolved_required_fields and (not norm.name or not norm.name.strip()):
            hard_rejection_reasons.append("Missing required product name")

        # Check required Unit Price & Numeric Validity
        if norm.unit_price is None:
            if "unit_price" in unresolved_required_fields:
                pass  # no trustworthy price column: reported through mapping_issues below
            elif norm.normalization_errors:
                hard_rejection_reasons.extend(norm.normalization_errors)
            else:
                hard_rejection_reasons.append("Missing required numeric unit_price")
        elif norm.unit_price < Decimal("0.0"):
            hard_rejection_reasons.append(f"Price cannot be negative: {norm.unit_price}")

        # If any hard rejection rule is violated, status is REJECTED immediately!
        # Assignment rule: "A JEV confidence value must not override a failed numeric or required-field validation."
        if hard_rejection_reasons:
            return ValidationOutcome(
                status="rejected",
                status_reason=" | ".join(hard_rejection_reasons),
                normalized_dict=self._build_normalized_dict(norm),
                reasons=hard_rejection_reasons,
            )

        # =========================================================================
        # 2. AMBIGUITY & QUALITY CHECKS (Produces 'needs_review')
        # =========================================================================

        # Check Currency Ambiguity (e.g., bare '$' without country context)
        if norm.is_currency_ambiguous:
            review_reasons.append(norm.currency_ambiguity_reason or "Ambiguous currency format")
        elif not norm.currency or norm.currency not in settings.supported_currency_list:
            review_reasons.append(
                f"Currency '{norm.currency}' is unsupported. Supported list: {settings.SUPPORTED_CURRENCIES}"
            )

        # Check optional measurements: unreadable values, then range (must be greater than zero)
        review_reasons.extend(norm.measurement_issues)
        for field_name, value, unit in (("payload_kg", norm.payload_kg, "kg"), ("reach_m", norm.reach_m, "m")):
            if value is not None and value <= 0:
                review_reasons.append(f"{field_name} must be greater than 0 (got {value} {unit})")

        # Check JEV column mapping: uncertain, conflicting or missing columns (per field, not averaged)
        review_reasons.extend(mapping_issues)

        # Check Duplicate Product within the same catalog spreadsheet
        if norm.sku:
            clean_sku = norm.sku.strip().upper()
            if clean_sku in self.seen_skus:
                prior_row = self.seen_skus[clean_sku]
                review_reasons.append(
                    f"Duplicate SKU '{norm.sku}' detected within catalog (first encountered at Excel Row {prior_row})"
                )
            else:
                self.seen_skus[clean_sku] = excel_row_index

        # If any review reason exists, status is NEEDS_REVIEW
        if review_reasons:
            normalized_payload = self._build_normalized_dict(norm)
            return ValidationOutcome(
                status="needs_review",
                status_reason=" | ".join(review_reasons),
                normalized_dict=normalized_payload,
                reasons=review_reasons,
            )

        # =========================================================================
        # 3. FULLY VALID RECORD (Produces 'ready')
        # =========================================================================
        normalized_payload = self._build_normalized_dict(norm)
        return ValidationOutcome(
            status="ready",
            status_reason=None,
            normalized_dict=normalized_payload,
        )

    def _build_normalized_dict(self, norm: NormalizedRowResult) -> Dict[str, Any]:
        """Serializes clean normalized values into dictionary for database storage."""
        return {
            "sku": norm.sku,
            "name": norm.name,
            "unit_price": str(norm.unit_price) if norm.unit_price is not None else None,
            "currency": norm.currency,
            "payload_kg": norm.payload_kg,
            "reach_m": norm.reach_m,
        }

