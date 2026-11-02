"""Authenticated Excel-only service, isolated from ingestion and alarm workers."""
from __future__ import annotations

import threading

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

import cloud_api


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_export_slot = threading.BoundedSemaphore(1)


class ExportResponse(StreamingResponse):
    """Keep the slot until generation and response delivery have both ended."""

    def __init__(self, response: StreamingResponse):
        super().__init__(response.body_iterator, status_code=response.status_code,
                         media_type=response.media_type, background=response.background)
        self.raw_headers = list(response.raw_headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                _export_slot.release()


@app.get("/api/v1/health")
def health():
    # Importing cloud_api does not run its startup/schema hooks in this app.
    return cloud_api.health()


@app.get("/api/history/export-xlsx")
def customer_excel_export(
    start: float | None = None,
    end: float | None = None,
    cleanroom_ids: str | None = None,
    device_ids: str | None = None,
    locale: str = Query(default="en-US", max_length=16),
    user: dict[str, object] = Depends(cloud_api.customer_user),
) -> StreamingResponse:
    if not _export_slot.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="已有导出正在进行，请完成后重试 / Another export is running; retry after it finishes",
            headers={"Retry-After": "5"},
        )
    try:
        response = cloud_api.customer_excel_export(
            start=start, end=end, cleanroom_ids=cleanroom_ids,
            device_ids=device_ids, locale=locale, user=user,
        )
        return ExportResponse(response)
    except BaseException:
        _export_slot.release()
        raise
