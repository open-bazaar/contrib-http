"""One HTTP listener that contribs register routes, applications and middleware on."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from contextvars import ContextVar
from typing import Protocol, Self
from urllib.parse import SplitResult, unquote_plus, urlsplit
lazy import asyncio
lazy import socket

from bazaar_contrib_timer import TIMER, Timer, timeout
from bazaar_core import Context, Contrib, ServiceKey
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route, Router, request_response
from starlette.types import ASGIApp, Receive, Scope, Send
lazy import uvicorn

__all__ = ["HTTP", "Endpoint", "Http", "HttpConfig", "Middleware", "http", "redact"]

type Endpoint = Callable[[Request], Awaitable[Response]]
type Middleware = Callable[[Request, RequestResponseEndpoint], Awaitable[Response]]

# going down comes first: requests still being answered get this long, as on the
# 0.2 server, before they are cancelled
SHUTDOWN_MS = 5_000
# and cancelled requests get this long to clean up before they are no longer
# waited for
CLEANUP_MS = 10_000


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # host and port, read the way a URL is: `127.0.0.1:8765`, `[::1]:8765`, or
    # the same with a scheme in front
    listen: str = "127.0.0.1:8765"

    @model_validator(mode="after")
    def _names_a_host_and_port(self) -> Self:
        try:
            parts = _parsed(self.listen)
            host, port = parts.hostname, parts.port
        except ValueError as error:
            raise ValueError(
                f"listen {self.listen!r} is not host:port: {error}"
            ) from error
        if not host or port is None:
            raise ValueError(f"listen {self.listen!r} is not host:port")
        return self

    @property
    def host(self) -> str:
        return _parsed(self.listen).hostname or ""

    @property
    def port(self) -> int:
        return _parsed(self.listen).port or 0


def _parsed(listen: str) -> SplitResult:
    return urlsplit(listen if "//" in listen else f"//{listen}")


class Http(Protocol):
    """Routes, applications and middleware held while their owner's context is open."""

    @property
    def url(self) -> str:
        """Where the listener answers, with the port it is bound to."""
        ...

    def route(
        self, path: str, endpoint: Endpoint, *, methods: Sequence[str]
    ) -> AbstractAsyncContextManager[None]:
        """Answer the path while open; leaving waits for its requests in flight."""
        ...

    def mount(self, path: str, app: ASGIApp) -> AbstractAsyncContextManager[None]:
        """Serve an ASGI application under the path while open; leaving waits for
        its requests in flight."""
        ...

    def middleware(self, dispatch: Middleware) -> AbstractAsyncContextManager[None]:
        """Wrap every request while open, the first registered outermost; leaving
        waits for the requests inside it."""
        ...


HTTP = ServiceKey[Http]("http")


def redact[E: Callable[..., object]](*names: str) -> Callable[[E], E]:
    """Write the values of these query parameters as `[redacted]` in the access
    log lines of requests routed to the endpoint."""

    def marked(endpoint: E) -> E:
        vars(endpoint)["redacted_query"] = frozenset(names)
        return endpoint

    return marked


_REQUEST = ContextVar[Scope]("request")


class _Redacted(logging.Filter):
    """Hides, in uvicorn's access log lines, the query values the routed endpoint
    marked with `redact`."""

    def filter(self, record: logging.LogRecord) -> bool:
        names: frozenset[str] = getattr(
            _REQUEST.get({}).get("endpoint"), "redacted_query", frozenset()
        )
        if names:
            # the arguments uvicorn's own access formatter unpacks
            assert isinstance(record.args, tuple)
            client, method, target, version, status = record.args
            path, separator, query = str(target).partition("?")
            parts = query.split("&")
            for index, part in enumerate(parts):
                name, _, value = part.partition("=")
                if value and unquote_plus(name) in names:
                    parts[index] = f"{name}=[redacted]"
            target = path + separator + "&".join(parts)
            record.args = (client, method, target, version, status)
        return True


class _InFlight:
    """The requests one registration is handling."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.idle = asyncio.Event()
        self.idle.set()

    async def run[T](self, call: Callable[[], Awaitable[T]]) -> T:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.idle.clear()
        try:
            return await call()
        finally:
            self.tasks.discard(task)
            if not self.tasks:
                self.idle.set()

    def serving(self, app: ASGIApp) -> ASGIApp:
        async def served(scope: Scope, receive: Receive, send: Send) -> None:
            await self.run(lambda: app(scope, receive, send))

        return served

    async def drain(self, timer: Timer) -> None:
        try:
            async with timeout(timer, SHUTDOWN_MS):
                await self.idle.wait()
        except TimeoutError:
            for task in self.tasks:
                task.cancel()
            with suppress(TimeoutError):
                async with timeout(timer, CLEANUP_MS):
                    await self.idle.wait()


class _Listener:
    def __init__(self, timer: Timer) -> None:
        self.timer = timer
        # a request is matched once it gets past the middleware, and the
        # registration it reaches counts it before anything awaits
        self.router = Router()
        self.middlewares: list[Callable[[ASGIApp], ASGIApp]] = []
        self.url = ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        _REQUEST.set(scope)
        app: ASGIApp = self.router
        for wrap in reversed(self.middlewares):
            app = wrap(app)
        await app(scope, receive, send)

    @asynccontextmanager
    async def route(
        self, path: str, endpoint: Endpoint, *, methods: Sequence[str]
    ) -> AsyncIterator[None]:
        requests = _InFlight()
        route = Route(path, endpoint, methods=list(methods))
        # Starlette serves anything but a plain function as an ASGI application
        route.app = requests.serving(request_response(endpoint))
        self.router.routes.append(route)
        try:
            yield
        finally:
            # routes compare equal by path, endpoint and methods
            self.router.routes = [
                registered
                for registered in self.router.routes
                if registered is not route
            ]
            await requests.drain(self.timer)

    @asynccontextmanager
    async def mount(self, path: str, app: ASGIApp) -> AsyncIterator[None]:
        requests = _InFlight()
        mounted = Mount(path, app=requests.serving(app))
        self.router.routes.append(mounted)
        try:
            yield
        finally:
            self.router.routes = [
                registered
                for registered in self.router.routes
                if registered is not mounted
            ]
            await requests.drain(self.timer)

    @asynccontextmanager
    async def middleware(self, dispatch: Middleware) -> AsyncIterator[None]:
        requests = _InFlight()

        def wrap(app: ASGIApp) -> ASGIApp:
            wrapped = requests.serving(BaseHTTPMiddleware(app, dispatch=dispatch))

            async def layer(scope: Scope, receive: Receive, send: Send) -> None:
                # dispatch sees HTTP requests only, and a request whose chain was
                # built before this left passes it by
                if scope["type"] == "http" and wrap in self.middlewares:
                    await wrapped(scope, receive, send)
                else:
                    await app(scope, receive, send)

            return layer

        self.middlewares.append(wrap)
        try:
            yield
        finally:
            self.middlewares.remove(wrap)
            await requests.drain(self.timer)


def _server(app: _Listener) -> uvicorn.Server:
    class Server(uvicorn.Server):
        @contextmanager
        def capture_signals(self) -> Iterator[None]:
            # SIGINT and SIGTERM belong to the host running this composition
            yield

    server = Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_config=None,
            timeout_graceful_shutdown=SHUTDOWN_MS // 1000,
        )
    )
    server.config.load()
    return server


@asynccontextmanager
async def _http(ctx: Context, config: HttpConfig) -> AsyncIterator[None]:
    listener = _Listener(ctx.accept(TIMER))
    family = socket.AF_INET6 if ":" in config.host else socket.AF_INET
    sock = await asyncio.to_thread(
        socket.create_server, (config.host, config.port), family=family
    )
    host, port = sock.getsockname()[:2]
    listener.url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    # loading the server imports its protocol implementations
    server = await asyncio.to_thread(_server, listener)
    access = logging.getLogger("uvicorn.access")
    redacted = _Redacted()
    access.addFilter(redacted)
    try:
        async with asyncio.TaskGroup() as tasks:
            serving = tasks.create_task(server.serve(sockets=[sock]))
            ctx.offer(HTTP, listener)
            try:
                yield
            finally:
                server.should_exit = True
                # on an error the task group would cancel serving before it closes
                # the listener
                try:
                    await asyncio.wait([serving])
                except asyncio.CancelledError:
                    # aborted: still close the listener, without waiting for the
                    # requests left on it
                    server.force_exit = True
                    await asyncio.wait([serving])
                    raise
    finally:
        access.removeFilter(redacted)


http = Contrib(config=HttpConfig, accepts=[TIMER], offers=[HTTP], lifespan=_http)
