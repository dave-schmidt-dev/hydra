"""Tests for hydra.web FastAPI route handlers (Phase 6 Task 6.1)."""

from __future__ import annotations

import asyncio
import errno
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hydra import state, web
from hydra.web import HydraApp, bind_socket, build_app


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    state._reset_breaker_for_tests()
    yield
    state._reset_breaker_for_tests()


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    state.init_session_db(tmp_path)
    return tmp_path


class StubDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[Any] = []
        self.run_called = False
        self.stopped = False

    async def enqueue_flag(self, flag) -> None:
        self.enqueued.append(flag)

    async def run(self) -> None:
        self.run_called = True
        # Block until cancelled — mimics real dispatcher.
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self.stopped = True


class StubIndexer:
    def __init__(self) -> None:
        self.index_called_with: list[Any] = []

    async def index(self, corpus_roots) -> dict:
        roots = list(corpus_roots)
        self.index_called_with.append(roots)
        return {}


@pytest.fixture
def hydra_app(session_dir: Path) -> HydraApp:
    return HydraApp(
        session_dir=session_dir,
        meeting_context="",
        dispatcher_inst=StubDispatcher(),
        indexer_inst=StubIndexer(),
    )


@pytest.fixture
def client(hydra_app: HydraApp) -> TestClient:
    app = build_app(hydra_app)
    return TestClient(app)


# ---------- GET / ----------


def test_root_renders_preflight_when_meeting_context_missing(
    client: TestClient, session_dir: Path
) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "pre-flight" in body.lower() or "preflight" in body.lower()


def test_root_renders_preflight_when_no_phase_set(
    client: TestClient, session_dir: Path
) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<form" in response.text


def test_root_renders_live_when_phase_live_and_context_exists(
    client: TestClient, hydra_app: HydraApp, session_dir: Path
) -> None:
    (session_dir / "hydra").mkdir(parents=True, exist_ok=True)
    (session_dir / "hydra" / "meeting_context.json").write_text(
        json.dumps({"meeting_about": "test"}), encoding="utf-8"
    )
    state.set_config(session_dir, "hydra.phase", "live")
    response = client.get("/")
    assert response.status_code == 200
    body = response.text.lower()
    assert "question" in body or "banner" in body


def test_root_renders_review_when_phase_review(
    client: TestClient, session_dir: Path
) -> None:
    (session_dir / "hydra").mkdir(parents=True, exist_ok=True)
    (session_dir / "hydra" / "meeting_context.json").write_text(
        json.dumps({"meeting_about": "test"}), encoding="utf-8"
    )
    state.set_config(session_dir, "hydra.phase", "review")
    response = client.get("/")
    assert response.status_code == 200
    assert "review" in response.text.lower() or "report" in response.text.lower()


def test_root_forces_preflight_when_phase_live_but_context_missing(
    client: TestClient, session_dir: Path
) -> None:
    state.set_config(session_dir, "hydra.phase", "live")
    response = client.get("/")
    assert response.status_code == 200
    assert "<form" in response.text


# ---------- POST /preflight ----------


def test_preflight_writes_meeting_context_and_transitions_phase(
    client: TestClient, session_dir: Path
) -> None:
    response = client.post(
        "/preflight",
        data={
            "meeting_about": "Project kickoff",
            "participants": "Alice, Bob",
            "corpus_paths": "",
            "obsidian_export_dir": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/")
    ctx_path = session_dir / "hydra" / "meeting_context.json"
    assert ctx_path.exists()
    ctx = json.loads(ctx_path.read_text())
    assert ctx["meeting_about"] == "Project kickoff"
    assert ctx["participants"] == ["Alice", "Bob"]
    assert state.get_config(session_dir, "hydra.phase") == "live"


def test_preflight_parses_corpus_paths_newline_separated(
    client: TestClient, session_dir: Path
) -> None:
    response = client.post(
        "/preflight",
        data={
            "meeting_about": "x",
            "participants": "",
            "corpus_paths": "/tmp/a\n/tmp/b\n",
            "obsidian_export_dir": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    ctx = json.loads((session_dir / "hydra" / "meeting_context.json").read_text())
    assert ctx["corpus_paths"] == ["/tmp/a", "/tmp/b"]


def test_preflight_stores_obsidian_export_dir(
    client: TestClient, session_dir: Path
) -> None:
    client.post(
        "/preflight",
        data={
            "meeting_about": "x",
            "participants": "",
            "corpus_paths": "",
            "obsidian_export_dir": "/tmp/vault",
        },
        follow_redirects=False,
    )
    ctx = json.loads((session_dir / "hydra" / "meeting_context.json").read_text())
    assert ctx["obsidian_export_dir"] == "/tmp/vault"


# ---------- POST /ask ----------


def test_ask_enqueues_flag_on_dispatcher(
    client: TestClient, hydra_app: HydraApp
) -> None:
    response = client.post("/ask", data={"topic": "capital of france"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["q_id"].startswith("q-")
    assert len(hydra_app.dispatcher_inst.enqueued) == 1
    flag = hydra_app.dispatcher_inst.enqueued[0]
    assert flag.topic == "capital of france"
    assert flag.source == "manual"
    assert flag.confidence == 1.0


def test_ask_rejects_empty_topic(client: TestClient) -> None:
    response = client.post("/ask", data={"topic": ""})
    assert response.status_code == 400


# ---------- POST /promote/{q_id} ----------


def test_promote_suggested_question_transitions_to_investigating(
    client: TestClient, hydra_app: HydraApp, session_dir: Path
) -> None:
    state.insert_question(
        session_dir,
        q_id="q-001",
        status="suggested",
        source="heuristic",
        topic="why?",
        rationale="r",
        confidence=0.5,
        transcript_window="ctx",
    )
    response = client.post("/promote/q-001")
    assert response.status_code == 200

    conn = state.open_session_db(session_dir)
    row = conn.execute("SELECT status FROM questions WHERE q_id = 'q-001'").fetchone()
    conn.close()
    assert row["status"] == "investigating"
    assert len(hydra_app.dispatcher_inst.enqueued) == 1


def test_promote_already_investigating_is_noop(
    client: TestClient, hydra_app: HydraApp, session_dir: Path
) -> None:
    state.insert_question(
        session_dir,
        q_id="q-001",
        status="investigating",
        source="heuristic",
        topic="why?",
        rationale="r",
        confidence=0.5,
        transcript_window="ctx",
    )
    response = client.post("/promote/q-001")
    assert response.status_code in (200, 409)
    if response.status_code == 200:
        body = response.json()
        assert body.get("status") in {"noop", "investigating", "already_investigating"}
    assert len(hydra_app.dispatcher_inst.enqueued) == 0


def test_promote_unknown_qid_returns_404(client: TestClient) -> None:
    response = client.post("/promote/q-999")
    assert response.status_code == 404


# ---------- POST /dismiss/{q_id} ----------


def test_dismiss_updates_status(client: TestClient, session_dir: Path) -> None:
    state.insert_question(
        session_dir,
        q_id="q-002",
        status="suggested",
        source="heuristic",
        topic="x",
    )
    response = client.post("/dismiss/q-002")
    assert response.status_code == 200

    conn = state.open_session_db(session_dir)
    row = conn.execute("SELECT status FROM questions WHERE q_id = 'q-002'").fetchone()
    conn.close()
    assert row["status"] == "dismissed"


def test_dismiss_unknown_qid_returns_404(client: TestClient) -> None:
    response = client.post("/dismiss/q-nope")
    assert response.status_code == 404


# ---------- POST /edit/{q_id} ----------


def test_edit_writes_user_edit_artifact_and_persists_notes(
    client: TestClient, session_dir: Path
) -> None:
    state.insert_question(
        session_dir,
        q_id="q-003",
        status="answered",
        source="heuristic",
        topic="x",
    )
    response = client.post("/edit/q-003", data={"notes": "first edit"})
    assert response.status_code == 200
    artifact = session_dir / "hydra" / "research" / "q-003.user-edit-1.md"
    assert artifact.exists()
    assert "first edit" in artifact.read_text()

    response2 = client.post("/edit/q-003", data={"notes": "second edit"})
    assert response2.status_code == 200
    artifact2 = session_dir / "hydra" / "research" / "q-003.user-edit-2.md"
    assert artifact2.exists()
    assert "second edit" in artifact2.read_text()

    conn = state.open_session_db(session_dir)
    row = conn.execute(
        "SELECT user_notes FROM questions WHERE q_id = 'q-003'"
    ).fetchone()
    conn.close()
    assert row["user_notes"] == "second edit"


def test_edit_unknown_qid_returns_404(client: TestClient) -> None:
    response = client.post("/edit/q-nope", data={"notes": "x"})
    assert response.status_code == 404


# ---------- POST /finalize ----------


def test_finalize_transitions_phase_to_review(
    client: TestClient, session_dir: Path
) -> None:
    response = client.post("/finalize")
    assert response.status_code == 200
    assert state.get_config(session_dir, "hydra.phase") == "review"


# ---------- POST /export ----------


def test_export_missing_report_returns_404(
    client: TestClient, session_dir: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "exported.md"
    response = client.post("/export", data={"destination": str(destination)})
    assert response.status_code == 404


def test_export_happy_path(
    client: TestClient, session_dir: Path, tmp_path: Path
) -> None:
    hydra_dir = session_dir / "hydra"
    hydra_dir.mkdir(parents=True, exist_ok=True)
    report = hydra_dir / "report.md"
    report.write_text("# Report\n\nbody", encoding="utf-8")
    destination = tmp_path / "out.md"

    response = client.post("/export", data={"destination": str(destination)})
    assert response.status_code == 200
    assert destination.exists()
    assert destination.read_text() == "# Report\n\nbody"
    assert state.get_config(session_dir, "hydra.last_export_path") == str(destination)


# ---------- bind_socket / PM-9 ----------


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_bind_socket_happy_path() -> None:
    port = _find_free_port()
    sock, actual = bind_socket(port=port)
    try:
        assert actual == port
        assert sock.getsockname()[1] == port
    finally:
        sock.close()


def test_bind_socket_ephemeral_fallback_when_all_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_socket = socket.socket

    call_state = {"attempts": 0}

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            self._real = real_socket(*args, **kwargs)

        def setsockopt(self, *args, **kwargs):
            return self._real.setsockopt(*args, **kwargs)

        def bind(self, address):
            _host, port = address
            call_state["attempts"] += 1
            if port != 0 and call_state["attempts"] <= web.PORT_BIND_ATTEMPTS:
                raise OSError(errno.EADDRINUSE, "in use")
            return self._real.bind(address)

        def listen(self, backlog):
            return self._real.listen(backlog)

        def getsockname(self):
            return self._real.getsockname()

        def close(self):
            return self._real.close()

    monkeypatch.setattr(web.socket, "socket", FakeSocket)
    sock, actual = bind_socket(port=web.DEFAULT_PORT)
    try:
        assert actual != web.DEFAULT_PORT
        assert actual > 0
    finally:
        sock.close()


def test_bind_socket_propagates_non_port_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_socket = socket.socket

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            self._real = real_socket(*args, **kwargs)

        def setsockopt(self, *args, **kwargs):
            return self._real.setsockopt(*args, **kwargs)

        def bind(self, address):
            raise OSError(errno.EPERM, "permission denied")

        def listen(self, backlog):
            return self._real.listen(backlog)

        def close(self):
            return self._real.close()

    monkeypatch.setattr(web.socket, "socket", FakeSocket)
    with pytest.raises(OSError) as excinfo:
        bind_socket(port=web.DEFAULT_PORT)
    assert excinfo.value.errno == errno.EPERM
