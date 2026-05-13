"""Tests for hydra.web SSE event streaming (Phase 6 Task 6.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hydra import state
from hydra.web import HydraApp, build_app


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state.init_session_db(tmp_path)
    return tmp_path


@pytest.fixture
def hydra_app(session_dir: Path) -> HydraApp:
    return HydraApp(session_dir=session_dir)


def _parse_data_line(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


def test_sse_connection_delivers_initial_connected_event(
    hydra_app: HydraApp,
) -> None:
    """Drive the SSE generator directly without TestClient stream blocking."""
    app = build_app(hydra_app)

    async def scenario() -> dict:
        # Find the /events route's endpoint and call it directly.
        events_route = next(
            r for r in app.routes if getattr(r, "path", None) == "/events"
        )
        endpoint = events_route.endpoint

        class FakeRequest:
            async def is_disconnected(self) -> bool:
                return False

        response = await endpoint(FakeRequest())
        # response is StreamingResponse; its body_iterator is the generator.
        gen = response.body_iterator
        try:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        finally:
            await gen.aclose()
        text = chunk if isinstance(chunk, str) else chunk.decode()
        line = text.splitlines()[0]
        return _parse_data_line(line)

    result = asyncio.run(scenario())
    assert result["type"] == "connected"
    assert "ts" in result


def test_broadcast_delivers_event_to_subscriber(hydra_app: HydraApp) -> None:
    async def scenario() -> dict:
        q: asyncio.Queue = asyncio.Queue()
        hydra_app.sse_subscribers.append(q)
        await hydra_app.broadcast("question_flagged", {"q_id": "q-001"})
        return await asyncio.wait_for(q.get(), timeout=1.0)

    result = asyncio.run(scenario())
    assert result["type"] == "question_flagged"
    assert result["q_id"] == "q-001"


def test_broadcast_delivers_to_all_subscribers(hydra_app: HydraApp) -> None:
    async def scenario() -> list[dict]:
        qs = [asyncio.Queue() for _ in range(3)]
        for q in qs:
            hydra_app.sse_subscribers.append(q)
        await hydra_app.broadcast("test_event", {"value": 42})
        return [await asyncio.wait_for(q.get(), timeout=1.0) for q in qs]

    results = asyncio.run(scenario())
    assert len(results) == 3
    for r in results:
        assert r["type"] == "test_event"
        assert r["value"] == 42


def test_broadcast_drops_events_when_subscriber_queue_full(
    hydra_app: HydraApp,
) -> None:
    async def scenario() -> int:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        hydra_app.sse_subscribers.append(q)
        for i in range(5):
            await hydra_app.broadcast("ping", {"n": i})
        return q.qsize()

    qsize = asyncio.run(scenario())
    assert qsize == 2


def test_sse_event_streams_broadcast_to_subscriber(hydra_app: HydraApp) -> None:
    """Subscribe via the actual /events generator and verify a broadcast reaches it."""
    app = build_app(hydra_app)

    async def scenario() -> dict:
        events_route = next(
            r for r in app.routes if getattr(r, "path", None) == "/events"
        )
        endpoint = events_route.endpoint

        class FakeRequest:
            async def is_disconnected(self) -> bool:
                return False

        response = await endpoint(FakeRequest())
        gen = response.body_iterator
        try:
            # Initial connected event.
            initial_chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            assert b"connected" in (
                initial_chunk
                if isinstance(initial_chunk, bytes)
                else initial_chunk.encode()
            )
            # Wait briefly so subscriber registration takes effect, then broadcast.
            await asyncio.sleep(0.01)
            await hydra_app.broadcast("question_flagged", {"q_id": "q-7"})
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        finally:
            await gen.aclose()
        text = chunk if isinstance(chunk, str) else chunk.decode()
        line = next(ln for ln in text.splitlines() if ln.startswith("data: "))
        return _parse_data_line(line)

    result = asyncio.run(scenario())
    assert result["type"] == "question_flagged"
    assert result["q_id"] == "q-7"


def test_subscriber_disconnect_cleans_up_queue(hydra_app: HydraApp) -> None:
    app = build_app(hydra_app)

    async def scenario() -> int:
        events_route = next(
            r for r in app.routes if getattr(r, "path", None) == "/events"
        )
        endpoint = events_route.endpoint

        class FakeRequest:
            async def is_disconnected(self) -> bool:
                return False

        response = await endpoint(FakeRequest())
        gen = response.body_iterator
        await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        registered = len(hydra_app.sse_subscribers)
        await gen.aclose()
        return registered

    registered = asyncio.run(scenario())
    assert registered == 1
    assert len(hydra_app.sse_subscribers) == 0
