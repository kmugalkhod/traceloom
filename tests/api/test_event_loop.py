"""Routes never run blocking database or store work on the event loop thread."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import sqlite3
import threading
import time

import httpx
from fastapi.routing import APIRoute
from sqlalchemy.engine import make_url

from traceloom.auth.permissions import Role

# Async on purpose: they stream the request body, then hand the work to the thread pool.
OFFLOADING_ROUTES = {"ingest", "export_traces"}


def _routes():
    """Every APIRoute of the routers main.include_discovered_routers mounts."""
    for pkg_name in ("traceloom.api", "traceloom.web.routers"):
        pkg = importlib.import_module(pkg_name)
        for info in pkgutil.iter_modules(pkg.__path__):
            router = getattr(importlib.import_module(f"{pkg_name}.{info.name}"), "router", None)
            yield from (r for r in getattr(router, "routes", []) if isinstance(r, APIRoute))


def test_no_async_route_does_sync_work_on_the_loop():
    routes = list(_routes())
    offenders = [
        f"{sorted(route.methods)} {route.path} ({route.endpoint.__name__})"
        for route in routes
        if inspect.iscoroutinefunction(route.endpoint)
        and route.endpoint.__name__ not in OFFLOADING_ROUTES
    ]
    assert len(routes) > 50 and offenders == []


async def test_a_write_waiting_on_a_locked_database_does_not_block_other_requests(
    app, client, settings, world, login
):
    login(client, world.user(Role.MEMBER))
    locker = sqlite3.connect(
        make_url(settings.database_url).database, isolation_level=None, check_same_thread=False
    )
    locker.execute("BEGIN IMMEDIATE")  # another writer holds the lock for 1.5 s
    release = threading.Timer(1.5, locker.commit)
    release.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={c.name: c.value for c in client.cookies.jar},
            headers=dict(client.headers),
        ) as ac:
            started = time.monotonic()
            write = asyncio.create_task(
                ac.post(f"/project/{world.project.id}/prefs/columns", data={"col": "name"})
            )
            await asyncio.sleep(0.2)
            health = await ac.get("/api/public/health")
            elapsed = time.monotonic() - started
            assert (await write).status_code == 303
    finally:
        release.join()
        locker.close()
    assert health.status_code == 200 and elapsed < 1.0


class _SlowLimiter:
    """A rate limiter whose store answers slowly, like Redis under load."""

    def __init__(self, inner):
        self.inner = inner

    def hit(self, key, limit):
        time.sleep(1.5)
        return self.inner.hit(key, limit)


async def test_a_slow_rate_limiter_does_not_block_other_requests(app, client, world, make_key):
    import base64

    _, public, secret = make_key(world.project)
    auth = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()
    container = app.state.container
    container.limiter = _SlowLimiter(container.limiter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        for path in ("/api/public/ingestion", "/api/public/otel/v1/traces"):
            started = time.monotonic()
            ingest = asyncio.create_task(
                ac.post(
                    path,
                    content=b"{}",
                    headers={"Authorization": auth, "Content-Type": "application/json"},
                )
            )
            await asyncio.sleep(0.2)
            health = await ac.get("/api/public/health")
            elapsed = time.monotonic() - started
            await ingest
            assert health.status_code == 200 and elapsed < 1.0, path
