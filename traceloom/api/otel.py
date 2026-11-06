"""POST /api/public/otel/v1/traces: OTLP/HTTP traces (JSON or protobuf), HTTP Basic project keys."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from traceloom.api.ingestion import read_body
from traceloom.auth.api_keys import ResolvedKey
from traceloom.auth.deps import enforce_ingest_rate_limit, project_key
from traceloom.ingest import otel
from traceloom.ingest.accept import (
    BatchTooLarge,
    IngestLimits,
    InvalidBatch,
    StorageUnavailable,
    accept_batch,
)

router = APIRouter(prefix="/api/public/otel/v1", tags=["otlp"])


@router.post("/traces")
async def export_traces(request: Request, key: ResolvedKey = Depends(project_key)) -> Response:
    enforce_ingest_rate_limit(request, key)
    container = request.app.state.container
    limits = IngestLimits(container.settings.ingest_max_events, container.settings.ingest_max_bytes)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in (otel.JSON_TYPE, otel.PROTOBUF_TYPE):
        raise HTTPException(415, "Content-Type must be application/json or application/x-protobuf.")
    try:
        body = await read_body(request, limits.max_bytes)
        body = otel.decompress(body, request.headers.get("content-encoding", ""), limits.max_bytes)
        if content_type == otel.PROTOBUF_TYPE:
            converted = otel.convert(otel.parse_protobuf(body))
        else:
            parsed, problems = otel.parse_json(body)
            converted = otel.convert(parsed, problems)
        rejected, errors = converted.rejected, list(converted.errors)
        if converted.events:
            raw = json.dumps({"batch": converted.events}, separators=(",", ":")).encode()
            result = await run_in_threadpool(
                accept_batch, key.project_id, raw, container.blob_store, container.queue, limits
            )
            rejected += len(result.errors)
            errors += [f"{e['id']}: {e['message']}" for e in result.errors[:5]]
    except (BatchTooLarge, otel.PayloadTooLarge) as exc:
        raise HTTPException(413, str(exc)) from exc
    except (InvalidBatch, otel.OtlpError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc
    payload, media_type = otel.response_body(content_type, rejected, "; ".join(errors[:5]))
    return Response(payload, media_type=media_type)
