from __future__ import annotations

from fastapi import APIRouter, Request, Response

from traceloom import __version__

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/health")
def health(request: Request, response: Response, deep: int = 0) -> dict:
    body: dict = {"status": "ok", "version": __version__}
    if deep:
        checks = {}
        for name, probe in request.app.state.container.health_checks.items():
            try:
                checks[name] = "ok" if probe() else "fail"
            except Exception:
                checks[name] = "fail"
        body["checks"] = checks
        if "fail" in checks.values():
            body["status"] = "degraded"
            response.status_code = 503
    return body
