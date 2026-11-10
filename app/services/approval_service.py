from decimal import Decimal
from typing import List, Optional, Set
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.catalog import StagingRecord, Product
from app.schemas.catalog import ApprovalSummary


class ApprovalService:
    """
    Handles approving eligible ('ready') staged records into the final products table.
    Enforces idempotency: a staged row is approved at most once, and a SKU is never inserted twice.
    """

    def approve_eligible_records(
        self,
        db: Session,
        staging_ids: Optional[List[int]] = None,
    ) -> ApprovalSummary:
        """
        Promotes staged records with status == 'ready' that have not been approved yet.
        Records requiring review or rejected are strictly excluded.
        """
        already_approved = db.query(Product.staging_record_id).filter(Product.staging_record_id.isnot(None))
        query = db.query(StagingRecord).filter(
            StagingRecord.status == "ready",
            ~StagingRecord.id.in_(already_approved),
        )
        if staging_ids:
            query = query.filter(StagingRecord.id.in_(staging_ids))
        eligible_records = query.order_by(StagingRecord.id).all()

        existing_skus: Set[str] = {sku for (sku,) in db.query(Product.sku)}
        newly_approved_count = 0
        skipped_existing_count = 0
        approved_skus: List[str] = []

        for record in eligible_records:
            data = record.normalized_data or {}
            sku = data.get("sku")
            if not sku:
                continue
            if sku in existing_skus:
                # Same SKU already approved (identical values, or ingestion would have sent it to review)
                skipped_existing_count += 1
                continue

            db.add(Product(
                sku=sku,
                name=data["name"],
                unit_price=Decimal(data["unit_price"]),
                currency=data["currency"],
                payload_kg=data.get("payload_kg"),
                reach_m=data.get("reach_m"),
                staging_record_id=record.id,
                source_filename=record.source_filename,
                source_row_index=record.source_row_index,
            ))
            existing_skus.add(sku)
            newly_approved_count += 1
            approved_skus.append(sku)

        try:
            db.commit()
        except IntegrityError:
            # Another approval inserted one of these SKUs concurrently: nothing is half-written
            db.rollback()
            raise

        message = (
            f"Approved {newly_approved_count} new product(s) out of {len(eligible_records)} eligible. "
            f"Skipped {skipped_existing_count} whose SKU is already in the catalog."
        )
        return ApprovalSummary(
            eligible_count=len(eligible_records),
            newly_approved_count=newly_approved_count,
            skipped_existing_count=skipped_existing_count,
            approved_skus=approved_skus,
            message=message,
        )
