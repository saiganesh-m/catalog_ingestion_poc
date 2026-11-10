from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Numeric, Float, Text, JSON, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.database import Base


def utc_now() -> datetime:
    """Helper to return current UTC timestamp."""
    return datetime.now(timezone.utc)


class StagingRecord(Base):
    """
    Stores incoming catalog rows after JEV mapping, normalization, and validation.
    Maintains complete data provenance including original coordinates and JEV decision audit trails.
    """
    __tablename__ = "staging_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Provenance Coordinates
    source_filename = Column(String(255), nullable=False)
    source_file_hash = Column(String(64), nullable=False, index=True)  # SHA-256 of the workbook bytes
    source_sheet = Column(String(100), nullable=False)
    source_row_index = Column(Integer, nullable=False)  # 1-indexed original Excel row number
    cell_coordinates = Column(JSON, nullable=True)      # {header: "A2", ...}

    # Raw and Processed Payloads
    raw_data = Column(JSON, nullable=False)            # Unparsed original key-values
    normalized_data = Column(JSON, nullable=True)      # Target schema values (kept even for rejected rows)
    normalization_notes = Column(JSON, nullable=True)  # Unit/currency conversions applied
    jev_decisions = Column(JSON, nullable=False)        # Mapping choice, probabilities, confidence, model

    # Staging Classification: 'ready', 'needs_review', 'rejected'
    status = Column(String(50), nullable=False, index=True)
    status_reason = Column(Text, nullable=True)         # Mandatory explanation for needs_review / rejected
    validation_results = Column(JSON, nullable=True)    # Every individual reason, as a list
    review_resolution = Column(JSON, nullable=True)     # Reviewer decision: action, corrections, note, previous status

    # Timestamps
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Relationship to approved product if promoted
    product = relationship("Product", back_populates="staging_record", uselist=False)

    __table_args__ = (
        # Re-importing the same file updates its staged rows instead of adding new ones
        UniqueConstraint("source_file_hash", "source_sheet", "source_row_index", name="uq_staging_source_row"),
        Index("ix_staging_source_row", "source_filename", "source_row_index"),
    )

    def __repr__(self) -> str:
        return f"<StagingRecord(id={self.id}, row={self.source_row_index}, status='{self.status}')>"


class Product(Base):
    """
    Final approved products table.
    Enforces SKU uniqueness to guarantee idempotent repeated imports.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Standardized Target Fields
    sku = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    unit_price = Column(Numeric(12, 4), nullable=False)
    currency = Column(String(10), nullable=False)
    payload_kg = Column(Float, nullable=True)
    reach_m = Column(Float, nullable=True)

    # Provenance Linking
    staging_record_id = Column(Integer, ForeignKey("staging_records.id"), nullable=True)
    source_filename = Column(String(255), nullable=False)
    source_row_index = Column(Integer, nullable=False)

    approved_at = Column(DateTime, default=utc_now, nullable=False)

    staging_record = relationship("StagingRecord", back_populates="product")

    def __repr__(self) -> str:
        return f"<Product(sku='{self.sku}', name='{self.name}', price={self.unit_price} {self.currency})>"

