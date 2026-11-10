from decimal import Decimal
from typing import Optional, Dict, Any, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class NormalizedProduct(BaseModel):
    """Normalized product structure adhering strictly to the assignment target fields."""
    model_config = ConfigDict(from_attributes=True)

    sku: str = Field(..., min_length=1, description="Required unique product identifier")
    name: str = Field(..., min_length=1, description="Required product name")
    unit_price: Decimal = Field(..., ge=0, description="Required decimal value, zero or greater")
    currency: str = Field(..., min_length=3, max_length=3, description="Required ISO 4217 currency code")
    payload_kg: Optional[float] = Field(None, ge=0, description="Optional payload in kilograms")
    reach_m: Optional[float] = Field(None, ge=0, description="Optional reach in metres")


class StagingRecordRead(BaseModel):
    """Schema for serialized staging record including full provenance and audit logs."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_filename: str
    source_file_hash: str
    source_sheet: str
    source_row_index: int
    cell_coordinates: Optional[Dict[str, str]] = None
    raw_data: Dict[str, Any]
    normalized_data: Optional[Dict[str, Any]] = None
    normalization_notes: Optional[List[str]] = None
    jev_decisions: Dict[str, Any]
    status: str
    status_reason: Optional[str] = None
    validation_results: Optional[List[str]] = None
    review_resolution: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class ProductRead(BaseModel):
    """Schema for final approved product record."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    unit_price: Decimal
    currency: str
    payload_kg: Optional[float] = None
    reach_m: Optional[float] = None
    source_filename: str
    source_row_index: int
    approved_at: datetime


class IngestionSummary(BaseModel):
    """High-level summary returned upon processing a catalog spreadsheet."""
    source_filename: str
    source_file_hash: str
    source_sheet: str
    total_rows: int
    new_rows: int                    # rows staged for the first time
    updated_rows: int                # rows already staged from an identical file, updated in place
    ready_count: int
    needs_review_count: int
    rejected_count: int
    column_mappings: Dict[str, str]
    jev_mode: str
    jev_model: str
    jev_notice: Optional[str] = None                                # Why the mock was used, if it was
    jev_evidence_files: List[str] = Field(default_factory=list)      # Saved live requests/responses (key redacted)
    mapping_issues: List[str] = Field(default_factory=list)          # Column-mapping problems sent to review


class ResolveRequest(BaseModel):
    """A reviewer's decision on a 'needs_review' record."""
    action: Literal["approve", "reject"]
    overrides: Dict[str, str] = Field(
        default_factory=dict,
        description="Corrected raw values, e.g. {\"currency\": \"USD\"} or {\"payload_kg\": \"12 kg\"}",
    )
    note: str = Field(..., min_length=1, description="Why the reviewer made this decision")


class ResolveResponse(BaseModel):
    """Outcome of a review decision."""
    staging_record: StagingRecordRead
    product: Optional[ProductRead] = None
    message: str


class ApprovalSummary(BaseModel):
    """Response returned when approving eligible staged records into products."""
    eligible_count: int              # 'ready' rows not yet approved
    newly_approved_count: int
    skipped_existing_count: int      # SKU already in products (identical values)
    approved_skus: List[str]
    message: str

