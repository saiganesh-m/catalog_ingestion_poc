"""
Saves every live JEV request/response as an audit file in output/jev_evidence/, with the API key redacted.

Mock answers are never written here: every file in this folder is a real exchange with the JEV API
(successful or failed), which is what the "evidence of a real JEV call" deliverable requires.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.config import settings

REDACTED = "***REDACTED***"


def save_jev_exchange(
    *,
    endpoint: str,
    request_payload: Dict[str, Any],
    status_code: Optional[int] = None,
    request_id: Optional[str] = None,
    response_body: Any = None,
    duration_ms: Optional[int] = None,
    attempts: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[str]:
    """Write one exchange to disk and return its path, or None if saving is disabled or fails."""
    if not settings.JEV_SAVE_EVIDENCE:
        return None

    captured_at = datetime.now(timezone.utc)
    record = {
        "captured_at": captured_at.isoformat(),
        "outcome": "success" if error is None else "failed",
        "request": {
            "method": "POST",
            "url": endpoint,
            "headers": {"Authorization": f"Bearer {REDACTED}", "Content-Type": "application/json"},
            "body": request_payload,
        },
        "response": {
            "status_code": status_code,
            "request_id": request_id,
            "duration_ms": duration_ms,
            "attempts": attempts,
            "body": response_body,
        },
        "error": error,
    }

    text = json.dumps(record, indent=2, ensure_ascii=False, default=str)
    api_key = settings.JEV_API_KEY.strip()
    if api_key:
        # Defence in depth: the key should never be in the record, but scrub it if it ever is.
        text = text.replace(api_key, REDACTED)

    evidence_dir = settings.OUTPUT_DIR / "jev_evidence"
    path = evidence_dir / f"jev_call_{captured_at:%Y%m%dT%H%M%S%fZ}.json"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None  # evidence is an audit aid; failing to write it must not break ingestion
    return str(path)
