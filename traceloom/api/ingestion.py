"""POST /api/public/ingestion: batched events, HTTP Basic project keys."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from traceloom.auth.api_keys import ResolvedKey
from traceloom.auth.deps import enforce_ingest_rate_limit, project_key
from traceloom.ingest.accept import (
    BatchTooLarge,
    IngestLimits,
    InvalidBatch,
    StorageUnavailable,
    accept_batch,
)

router = APIRouter(prefix="/api/public", tags=["public"])


async def read_body(request: Request, max_bytes: int) -> bytes:
    """The request body, refusing to buffer more than `max_bytes`."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise BatchTooLarge(f"Request body exceeds {max_bytes} bytes.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise BatchTooLarge(f"Request body exceeds {max_bytes} bytes.")
    return bytes(body)


@router.post("/ingestion", status_code=207)
async def ingest(request: Request, key: ResolvedKey = Depends(project_key)) -> JSONResponse:
    enforce_ingest_rate_limit(request, key)
    container = request.app.state.container
    limits = IngestLimits(container.settings.ingest_max_events, container.settings.ingest_max_bytes)
    try:
        raw = await read_body(request, limits.max_bytes)
        result = await run_in_threadpool(
            accept_batch, key.project_id, raw, container.blob_store, container.queue, limits
        )
    except BatchTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except InvalidBatch as exc:
        raise HTTPException(400, str(exc)) from exc
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc
    return JSONResponse(result.body(), status_code=207)
