from __future__ import annotations

import logging
lazy import asyncio
lazy from collections.abc import AsyncIterator
lazy from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from bazaar_contrib_timer.wheel import timer_wheel
from bazaar_core import Composition, Context, Contrib, ContribEntry, ContribHub
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route as StarletteRoute

from bazaar_contrib_http import (
    CLEANUP_MS,
    HTTP,
    SHUTDOWN_MS,
    HttpConfig,
    http,
    redact,
)


def listening(listen: str = "127.0.0.1:0") -> Composition:
    return Composition(
        entries=[
            ContribEntry(name="timer", definition=timer_wheel),
            ContribEntry(name="http", definition=http, config={"listen": listen}),
        ]
    )


def owner(lifespan) -> Composition:
    return Composition(
        entries=[
            ContribEntry(
                name="owner", definition=Contrib(accepts=[HTTP], lifespan=lifespan)
            )
        ]
    )


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # building the client loads certificates from disk, which is not the loop's work
    async with await asyncio.to_thread(httpx.AsyncClient, timeout=30) as session:
        yield session


async def test_a_route_is_answered_while_its_contrib_runs():
    seen: dict[str, str] = {}

    @asynccontextmanager
    async def greeter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def hello(request: Request) -> Response:
            return PlainTextResponse(f"hello {request.query_params['name']}")

        async with web.route("/hello", hello, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        async with hub.enable(owner(greeter)):
            answered = await session.get(
                f"{seen['url']}/hello", params={"name": "kana"}
            )
            assert answered.status_code == 200
            assert "kana" in answered.text
        gone = await session.get(f"{seen['url']}/hello", params={"name": "kana"})
        assert gone.status_code == 404


async def test_an_endpoint_object_with_a_call_method_is_answered():
    seen: dict[str, str] = {}

    class Greeter:
        async def __call__(self, request: Request) -> Response:
            return PlainTextResponse(f"hello {request.query_params['name']}")

    @asynccontextmanager
    async def greeter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url
        async with web.route("/hello", Greeter(), methods=["GET"]):
            yield

    async with (
        ContribHub() as hub,
        hub.enable(listening()),
        hub.enable(owner(greeter)),
        client() as session,
    ):
        answered = await session.get(f"{seen['url']}/hello", params={"name": "kana"})
    assert answered.status_code == 200
    assert "kana" in answered.text


async def test_a_mounted_application_is_served_under_its_path_while_its_contrib_runs():
    seen: dict[str, str] = {}

    async def items(request: Request) -> Response:
        return PlainTextResponse(f"stock for {request.path_params['sku']}")

    inventory = Starlette(routes=[StarletteRoute("/items/{sku}", items)])

    @asynccontextmanager
    async def shop(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url
        async with web.mount("/inventory", inventory):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        async with hub.enable(owner(shop)):
            answered = await session.get(f"{seen['url']}/inventory/items/A-17")
            assert answered.status_code == 200
            assert "A-17" in answered.text
        gone = await session.get(f"{seen['url']}/inventory/items/A-17")
        assert gone.status_code == 404


async def test_middleware_wraps_requests_with_the_first_registered_outermost():
    seen: dict[str, str] = {}
    order: list[str] = []

    def wrapper(name: str):
        async def dispatch(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            order.append(f"{name} in")
            response = await call_next(request)
            order.append(f"{name} out")
            return response

        return dispatch

    @asynccontextmanager
    async def wrapped(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def report(request: Request) -> Response:
            order.append("route")
            return PlainTextResponse("saved")

        async with (
            web.middleware(wrapper("outer")),
            web.middleware(wrapper("inner")),
            web.route("/reports", report, methods=["POST"]),
        ):
            yield

    async with (
        ContribHub() as hub,
        hub.enable(listening()),
        hub.enable(owner(wrapped)),
        client() as session,
    ):
        response = await session.post(f"{seen['url']}/reports")
    assert response.status_code == 200
    assert order == ["outer in", "inner in", "route", "inner out", "outer out"]


async def test_leaving_waits_for_the_requests_it_is_answering():
    seen: dict[str, str] = {}
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def exporter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def export(request: Request) -> Response:
            started.set()
            await release.wait()
            order.append("answered")
            return PlainTextResponse("exported")

        async with web.route("/export", export, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(exporter)))
        request = asyncio.create_task(session.get(f"{seen['url']}/export"))
        await started.wait()

        async def leave() -> None:
            await stack.aclose()
            order.append("left")

        leaving = asyncio.create_task(leave())
        await asyncio.sleep(0.2)
        release.set()
        response, _ = await asyncio.gather(request, leaving)
    assert response.status_code == 200
    assert order == ["answered", "left"]


async def test_two_registrations_of_one_endpoint_each_wait_for_their_own_requests():
    seen: dict[str, str] = {}
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def export(request: Request) -> Response:
        started.set()
        await release.wait()
        order.append("answered")
        return PlainTextResponse("exported")

    @asynccontextmanager
    async def exporter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url
        async with web.route("/export", export, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        first = AsyncExitStack()
        await first.enter_async_context(hub.enable(owner(exporter)))
        second = AsyncExitStack()
        await second.enter_async_context(hub.enable(owner(exporter)))
        await second.aclose()
        request = asyncio.create_task(session.get(f"{seen['url']}/export"))
        await started.wait()

        async def leave() -> None:
            await first.aclose()
            order.append("left")

        leaving = asyncio.create_task(leave())
        await asyncio.sleep(0.2)
        release.set()
        response, _ = await asyncio.gather(request, leaving)
    assert response.status_code == 200
    assert order == ["answered", "left"]


async def test_leaving_waits_for_a_streamed_response_to_finish():
    seen: dict[str, str] = {}
    streaming = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def exporter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def rows() -> AsyncIterator[str]:
            yield "first\n"
            streaming.set()
            await release.wait()
            yield "last\n"
            order.append("streamed")

        async def export(request: Request) -> Response:
            return StreamingResponse(rows(), media_type="text/plain")

        async with web.route("/export", export, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(exporter)))
        request = asyncio.create_task(session.get(f"{seen['url']}/export"))
        await streaming.wait()

        async def leave() -> None:
            await stack.aclose()
            order.append("left")

        leaving = asyncio.create_task(leave())
        await asyncio.sleep(0.2)
        release.set()
        response, _ = await asyncio.gather(request, leaving)
    assert "last" in response.text
    assert order == ["streamed", "left"]


async def test_leaving_cancels_requests_that_outlast_the_shutdown_budget_and_waits_for_their_cleanup():
    seen: dict[str, str] = {}
    started = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def stuck(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def wait_forever(request: Request) -> Response:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.1)
                order.append("cleaned up")
            return PlainTextResponse("never")

        async with web.route("/stuck", wait_forever, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(stuck)))
        request = asyncio.create_task(session.get(f"{seen['url']}/stuck"))
        await started.wait()
        loop = asyncio.get_running_loop()
        began = loop.time()
        await stack.aclose()
        order.append("left")
        waited_ms = (loop.time() - began) * 1000
        response = await request
    assert response.status_code >= 500
    assert waited_ms >= SHUTDOWN_MS / 2
    assert waited_ms < SHUTDOWN_MS * 3
    assert order == ["cleaned up", "left"]


async def test_leaving_stops_waiting_for_a_cancelled_request_that_is_still_cleaning_up():
    seen: dict[str, str] = {}
    started = asyncio.Event()
    unblock = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def stuck(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def wait_forever(request: Request) -> Response:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await unblock.wait()
                order.append("cleaned up")
            return PlainTextResponse("never")

        async with web.route("/stuck", wait_forever, methods=["GET"]):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(stuck)))
        request = asyncio.create_task(session.get(f"{seen['url']}/stuck"))
        await started.wait()
        loop = asyncio.get_running_loop()
        began = loop.time()
        try:
            async with asyncio.timeout((SHUTDOWN_MS + CLEANUP_MS) * 2 / 1000):
                await stack.aclose()
            order.append("left")
            waited_ms = (loop.time() - began) * 1000
        finally:
            unblock.set()
        await request
    assert order == ["left", "cleaned up"]
    assert waited_ms >= (SHUTDOWN_MS + CLEANUP_MS) / 2


async def test_registrations_left_while_a_request_waits_in_middleware_are_not_reached():
    seen: dict[str, str] = {}
    waiting = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def gate(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def hold(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            waiting.set()
            await release.wait()
            return await call_next(request)

        async with web.middleware(hold):
            yield

    @asynccontextmanager
    async def reports(ctx: Context):
        web = ctx.accept(HTTP)

        async def tag(request: Request, call_next: RequestResponseEndpoint) -> Response:
            order.append("tagged")
            return await call_next(request)

        async def report(request: Request) -> Response:
            order.append("reported")
            return PlainTextResponse("report")

        async with web.middleware(tag), web.route("/report", report, methods=["GET"]):
            yield

    async with (
        ContribHub() as hub,
        hub.enable(listening()),
        hub.enable(owner(gate)),
        client() as session,
    ):
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(reports)))
        request = asyncio.create_task(session.get(f"{seen['url']}/report"))
        await waiting.wait()
        await stack.aclose()
        release.set()
        response = await request
    assert response.status_code == 404
    assert order == []


async def test_leaving_middleware_waits_for_the_requests_inside_it():
    seen: dict[str, str] = {}
    inside = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def pinger(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def ping(request: Request) -> Response:
            return PlainTextResponse("pong")

        async with web.route("/ping", ping, methods=["GET"]):
            yield

    @asynccontextmanager
    async def auditor(ctx: Context):
        async def audit(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            response = await call_next(request)
            inside.set()
            await release.wait()
            order.append("audited")
            return response

        async with ctx.accept(HTTP).middleware(audit):
            yield

    async with (
        ContribHub() as hub,
        hub.enable(listening()),
        hub.enable(owner(pinger)),
        client() as session,
    ):
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(auditor)))
        request = asyncio.create_task(session.get(f"{seen['url']}/ping"))
        await inside.wait()

        async def leave() -> None:
            await stack.aclose()
            order.append("left")

        leaving = asyncio.create_task(leave())
        await asyncio.sleep(0.2)
        release.set()
        response, _ = await asyncio.gather(request, leaving)
    assert response.status_code == 200
    assert order == ["audited", "left"]


async def test_leaving_middleware_waits_for_the_response_it_streams():
    seen: dict[str, str] = {}
    streaming = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    @asynccontextmanager
    async def exporter(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def rows() -> AsyncIterator[str]:
            yield "first\n"
            streaming.set()
            await release.wait()
            yield "last\n"
            order.append("streamed")

        async def export(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            return StreamingResponse(rows(), media_type="text/plain")

        async with web.middleware(export):
            yield

    async with ContribHub() as hub, hub.enable(listening()), client() as session:
        stack = AsyncExitStack()
        await stack.enter_async_context(hub.enable(owner(exporter)))
        request = asyncio.create_task(session.get(f"{seen['url']}/export"))
        await streaming.wait()

        async def leave() -> None:
            await stack.aclose()
            order.append("left")

        leaving = asyncio.create_task(leave())
        await asyncio.sleep(0.2)
        release.set()
        response, _ = await asyncio.gather(request, leaving)
    assert "last" in response.text
    assert order == ["streamed", "left"]


async def test_access_logs_hide_the_values_of_query_parameters_an_endpoint_marks(
    caplog: pytest.LogCaptureFixture,
):
    seen: dict[str, str] = {}

    @redact("code", "state")
    async def callback(request: Request) -> Response:
        return PlainTextResponse("signed in")

    @redact("token")
    async def invite(request: Request) -> Response:
        return PlainTextResponse("joined")

    @asynccontextmanager
    async def signin(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url
        teams = Starlette(routes=[StarletteRoute("/invite", invite)])
        async with (
            web.route("/callback", callback, methods=["GET"]),
            web.mount("/teams", teams),
        ):
            yield

    caplog.set_level(logging.INFO, logger="uvicorn.access")
    async with (
        ContribHub() as hub,
        hub.enable(listening()),
        hub.enable(owner(signin)),
        client() as session,
    ):
        url = seen["url"]
        await session.get(
            f"{url}/callback",
            params={"code": "one-time-code", "state": "opaque-state", "page": "2"},
        )
        await session.post(f"{url}/callback", params={"code": "posted-code"})
        await session.get(
            f"{url}/teams/invite",
            params={"token": "invite-token", "team": "night-market"},
        )
    lines = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "uvicorn.access"
    )
    assert "code=" in lines
    assert "page=2" in lines
    assert "team=night-market" in lines
    for secret in ("one-time-code", "opaque-state", "posted-code", "invite-token"):
        assert secret not in lines


async def test_stopping_closes_the_listener():
    seen: dict[str, str] = {}

    @asynccontextmanager
    async def watcher(ctx: Context):
        seen["url"] = ctx.accept(HTTP).url
        yield

    async with client() as session:
        async with (
            ContribHub() as hub,
            hub.enable(listening()),
            hub.enable(owner(watcher)),
        ):
            reachable = await session.get(f"{seen['url']}/anything")
        assert reachable.status_code == 404
        with pytest.raises(httpx.ConnectError):
            await session.get(f"{seen['url']}/anything")


async def test_an_aborted_stop_still_closes_the_listener():
    seen: dict[str, str] = {}
    ready = asyncio.Event()
    stop = asyncio.Event()
    lingering = asyncio.Event()

    @asynccontextmanager
    async def slow(ctx: Context):
        seen["url"] = ctx.accept(HTTP).url
        ready.set()
        try:
            yield
        finally:
            lingering.set()
            await asyncio.Event().wait()

    composition = Composition(
        entries=[
            *listening().entries,
            ContribEntry(
                name="slow", definition=Contrib(accepts=[HTTP], lifespan=slow)
            ),
        ]
    )

    async with ContribHub() as hub, client() as session:

        async def run() -> None:
            async with hub.enable(composition):
                await stop.wait()

        running = asyncio.create_task(run())
        await ready.wait()
        stop.set()
        await lingering.wait()
        # cancelling whoever is stopping the composition aborts the contribs
        # that have not stopped yet, http among them
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        with pytest.raises(httpx.ConnectError):
            await session.get(f"{seen['url']}/anything")


async def test_an_ipv6_listen_address_is_served():
    seen: dict[str, str] = {}

    @asynccontextmanager
    async def pinger(ctx: Context):
        web = ctx.accept(HTTP)
        seen["url"] = web.url

        async def ping(request: Request) -> Response:
            return PlainTextResponse("pong")

        async with web.route("/ping", ping, methods=["GET"]):
            yield

    async with (
        ContribHub() as hub,
        hub.enable(listening("[::1]:0")),
        hub.enable(owner(pinger)),
        client() as session,
    ):
        response = await session.get(f"{seen['url']}/ping")
    assert "[::1]" in seen["url"]
    assert response.status_code == 200


def test_listen_must_name_a_host_and_a_port():
    for listen in ("127.0.0.1:8765", "[::1]:8765", "http://0.0.0.0:80"):
        assert HttpConfig(listen=listen).port > 0
    for listen in ("localhost", "127.0.0.1:port", ":8765"):
        with pytest.raises(ValidationError):
            HttpConfig(listen=listen)
