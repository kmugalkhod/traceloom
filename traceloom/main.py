"""App factory: middleware, lifespan and router auto-discovery."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from traceloom import __version__
from traceloom.api.errors import install_error_handlers
from traceloom.auth.csrf import CSRFMiddleware
from traceloom.auth.sessions import SessionMiddleware
from traceloom.config import Settings, get_settings
from traceloom.container import build_container

CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware:
    """Pure ASGI, so the headers land on every response including errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message["headers"]
                    if k.decode().lower() not in SECURITY_HEADERS
                ]
                headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def include_discovered_routers(app: FastAPI) -> None:
    """Include `router` from every module in traceloom.api and traceloom.web.routers."""
    for pkg_name in ("traceloom.api", "traceloom.web.routers"):
        pkg = importlib.import_module(pkg_name)
        for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
            module = importlib.import_module(f"{pkg_name}.{info.name}")
            router = getattr(module, "router", None)
            if router is not None:
                app.include_router(router)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    container = build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container.startup()
        try:
            yield
        finally:
            container.shutdown()

    app = FastAPI(title="Traceloom", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.container = container
    # Last added runs first: headers wrap everything, then CSRF, then the session cookie check.
    app.add_middleware(SessionMiddleware, settings=settings)
    app.add_middleware(CSRFMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    install_error_handlers(app)
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    include_discovered_routers(app)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy module attribute so importing traceloom.main never reads the environment.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
