from typing import Optional
from zipfile import BadZipFile

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, status
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.core.config import settings
from app.schemas.catalog import IngestionSummary
from app.services.ingestion_service import IngestionService
from app.services.jev import JevApiError

router = APIRouter(prefix="/api/v1/ingest", tags=["Catalog Ingestion"])


@router.post("/upload", response_model=IngestionSummary, status_code=status.HTTP_201_CREATED)
def upload_catalog(
    file: UploadFile = File(..., description="Excel catalog workbook (.xlsx)"),
    sheet: Optional[str] = Query(None, description="Worksheet to import (default: the active sheet)"),
    db: Session = Depends(get_db),
):
    """
    Upload and process an Excel catalog workbook.
    - Extracts headers and cell coordinates using openpyxl.
    - Queries JEV for typed semantic column mappings.
    - Deterministically normalizes prices, currencies, and units.
    - Validates data integrity and stages records as 'ready', 'needs_review', or 'rejected'.
    """
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only Excel workbooks (.xlsx) are supported.",
        )

    try:
        content = file.file.read()
        # A fresh service per request: the validator tracks duplicate SKUs per file
        summary = IngestionService().process_catalog(
            source=content,
            filename=file.filename,
            db=db,
            sheet_name=sheet,
        )
        return summary
    except (ValueError, BadZipFile, InvalidFileException) as e:
        # Unreadable workbook, missing header row or unknown sheet: the client's input is at fault
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot read workbook: {e}")
    except JevApiError as e:
        # Only reachable with JEV_MODE=live, where falling back to the mock is not allowed
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"JEV is unavailable (JEV_MODE=live, no mock fallback): {e}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Catalog processing failed: {str(e)}",
        )


@router.get("/health")
def health_check():
    """Returns application status, JEV connection mode, and active currency configuration."""
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "jev_mode": settings.JEV_MODE,
        "jev_live_calls_enabled": settings.is_jev_live,
        "jev_endpoint": f"{settings.JEV_API_BASE_URL.rstrip('/')}/systemone",
        "jev_model": settings.JEV_MODEL,
        "supported_currencies": settings.supported_currency_list,
        "confidence_threshold": settings.CONFIDENCE_THRESHOLD,
    }
