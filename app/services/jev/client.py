"""
HTTP transport for TypeSafe AI's System One endpoint: POST {JEV_API_BASE_URL}/systemone.

Uses the operating-system certificate store for TLS, retries transient failures
(408, 429, 5xx, connection errors, timeouts) and turns HTTP errors into readable messages.
"""
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
import truststore

from app.core.config import settings

RETRYABLE_STATUSES = {408, 429} | set(range(500, 600))

ERROR_HINTS = {
    401: "API key missing or invalid - check JEV_API_KEY in .env",
    403: "Access forbidden - the API key is not authorised for this endpoint",
    404: "Endpoint not found - check JEV_API_BASE_URL in .env",
    422: "Request rejected as malformed - the response body names the offending field",
    429: "Rate limit exceeded",
    529: "JEV is temporarily overloaded",
}


class JevApiError(Exception):
    """Raised when the JEV API cannot return a usable answer."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.body = body


@dataclass
class JevHttpResult:
    """A successful System One call plus the metadata worth auditing."""
    response: Dict[str, Any]
    status_code: int
    request_id: Optional[str]
    duration_ms: int
    attempts: int


class JevClient:
    """Thin, retrying client for the System One endpoint."""

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        self.endpoint = f"{settings.JEV_API_BASE_URL.rstrip('/')}/systemone"
        self.timeout = settings.JEV_TIMEOUT_SECONDS
        self.max_retries = settings.JEV_MAX_RETRIES
        self.backoff_initial = 0.5
        self._transport = transport  # injected in tests; None means real network

    def system_one(self, payload: Dict[str, Any]) -> JevHttpResult:
        """Send one System One request. Raises JevApiError when no usable answer is returned."""
        headers = {
            "Authorization": f"Bearer {settings.JEV_API_KEY.strip()}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        last_error: Optional[JevApiError] = None

        with self._build_http_client() as client:
            for attempt in range(1, self.max_retries + 2):
                retry_after: Optional[float] = None
                try:
                    response = client.post(self.endpoint, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_error = JevApiError(f"Could not reach JEV at {self.endpoint}: {type(exc).__name__}: {exc}")
                else:
                    request_id = response.headers.get("x-typesafe-request-id")
                    if response.is_success:
                        try:
                            body = response.json()
                        except ValueError:
                            raise JevApiError(
                                "JEV returned a response that is not valid JSON",
                                response.status_code, request_id, response.text[:500],
                            )
                        duration_ms = int((time.monotonic() - started) * 1000)
                        return JevHttpResult(body, response.status_code, request_id, duration_ms, attempt)

                    last_error = self._http_error(response, request_id)
                    if response.status_code not in RETRYABLE_STATUSES:
                        raise last_error
                    retry_after = self._retry_after_seconds(response)

                if attempt <= self.max_retries:
                    time.sleep(retry_after if retry_after is not None else self.backoff_initial * 2 ** (attempt - 1))

        raise last_error

    def _build_http_client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=self.timeout)
        # truststore validates certificates against the OS store (Windows/macOS/Linux), which fixes
        # CERTIFICATE_VERIFY_FAILED errors on machines where antivirus or a proxy re-signs HTTPS traffic.
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return httpx.Client(verify=ssl_context, timeout=self.timeout)

    @staticmethod
    def _http_error(response: httpx.Response, request_id: Optional[str]) -> JevApiError:
        hint = ERROR_HINTS.get(response.status_code, "Unexpected HTTP error")
        body = response.text[:500]
        return JevApiError(f"HTTP {response.status_code}: {hint}. Body: {body}", response.status_code, request_id, body)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
        """Honour the server's retry hint, capped at 10 seconds."""
        try:
            if "retry-after-ms" in response.headers:
                return min(float(response.headers["retry-after-ms"]) / 1000, 10.0)
            if "retry-after" in response.headers:
                return min(float(response.headers["retry-after"]), 10.0)
        except ValueError:
            pass
        return None
