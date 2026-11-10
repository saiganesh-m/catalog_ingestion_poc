from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.models.catalog import Product
from app.schemas.catalog import ProductRead, ApprovalSummary
from app.services.approval_service import ApprovalService

router = APIRouter(prefix="/api/v1/products", tags=["Approved Products"])
approval_service = ApprovalService()


@router.post("/approve", response_model=ApprovalSummary, status_code=status.HTTP_200_OK)
def approve_products(
    staging_ids: Optional[List[int]] = Query(None, description="Optional specific staged IDs to approve"),
    db: Session = Depends(get_db),
):
    """
    Approve eligible ('ready') staged records and insert them into the products table.
    Enforces idempotency: repeated approvals will not duplicate products.
    Records requiring review or rejected are strictly barred.
    """
    summary = approval_service.approve_eligible_records(db=db, staging_ids=staging_ids)
    return summary


@router.get("", response_model=List[ProductRead])
def list_approved_products(
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Retrieve all approved products stored in the final production database."""
    products = db.query(Product).order_by(Product.id.asc()).limit(limit).all()
    return products

