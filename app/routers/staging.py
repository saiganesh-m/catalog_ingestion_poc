from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.models.catalog import StagingRecord
from app.schemas.catalog import ResolveRequest, ResolveResponse, StagingRecordRead
from app.services.review_service import ResolutionError, ReviewService

router = APIRouter(prefix="/api/v1/staging", tags=["Staging Area"])


@router.get("", response_model=List[StagingRecordRead])
def list_staged_records(
    status: Optional[str] = Query(None, description="Filter by status: ready, needs_review, or rejected"),
    filename: Optional[str] = Query(None, description="Filter by source filename"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    List processed catalog records from the staging area.
    Provides complete provenance, raw unparsed values, and JEV decision details.
    """
    query = db.query(StagingRecord)
    if status:
        query = query.filter(StagingRecord.status == status.lower().strip())
    if filename:
        query = query.filter(StagingRecord.source_filename == filename)

    records = query.order_by(StagingRecord.id.asc()).limit(limit).all()
    return records


@router.get("/{record_id}", response_model=StagingRecordRead)
def get_staged_record_detail(
    record_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve full audit provenance for a specific staged catalog row."""
    record = db.query(StagingRecord).filter(StagingRecord.id == record_id).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Staging record with ID {record_id} not found",
        )
    return record


@router.post("/{record_id}/resolve", response_model=ResolveResponse)
def resolve_review(
    record_id: int,
    decision: ResolveRequest,
    db: Session = Depends(get_db),
):
    """
    Resolve a 'needs_review' record: approve it (optionally correcting values such as the currency)
    or reject it. Corrections are re-parsed and re-validated; an approval goes straight into products.
    """
    try:
        record, product = ReviewService().resolve(
            db, record_id, decision.action, decision.note, decision.overrides
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    message = (
        f"Record {record_id} approved as product '{product.sku}'" if product
        else f"Record {record_id} rejected by reviewer"
    )
    return ResolveResponse(staging_record=record, product=product, message=message)

