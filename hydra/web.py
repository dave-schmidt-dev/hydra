"""FastAPI web layer for Hydra (Phase 6).

Routes per plan Section 4.5.7: htmx-driven templates + SSE for live updates.
Wires the watcher, dispatcher, indexer, and tailer into a single asyncio
event loop; tests inject stubs via ``HydraApp``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import itertools
import json
import logging
import shutil
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hydra import dispatcher, indexer, quota, state, tailer
from hydra.watcher import Flag

logger = logging.getLogger("hydra.web")

DEFAULT_PORT = 4125
PORT_BIND_ATTEMPTS = 10  # PM-9: try 10 sequential ports, then fall back to ephemeral

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_SSE_QUEUE_MAXSIZE = 64
_MANUAL_Q_COUNTER = itertools.count(1)


def _next_manual_q_id() -> str:
    return f"q-manual-{next(_MANUAL_Q_COUNTER):04d}"


@dataclass
class HydraApp:
    """Wiring for the FastAPI app. Tests stub dispatcher_inst / indexer_inst."""

    session_dir: Path
    meeting_context: str = ""
    quota_router: quota.QuotaRouter | None = None
    dispatcher_inst: dispatcher.Dispatcher | None = None
    indexer_inst: indexer.Indexer | None = None
    tailer_inst: tailer.TranscriptTailer | None = None
    watcher_task: asyncio.Task | None = None
    dispatcher_task: asyncio.Task | None = None
    indexer_task: asyncio.Task | None = None
    sse_subscribers: list[asyncio.Queue] = field(default_factory=list)
    started_at: datetime | None = None

    async def broadcast(self, event_type: str, payload: dict) -> None:
        event = {"type": event_type, **payload}
        for q in list(self.sse_subscribers):
            # Slow subscriber: drop the event rather than block the loop.
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)


# --- Port binding (PM-9) ---


def bind_socket(
    host: str = "127.0.0.1", port: int = DEFAULT_PORT
) -> tuple[socket.socket, int]:
    """Bind to ``port`` (or the next free port within PORT_BIND_ATTEMPTS).

    Per PM-9: if all sequential candidates fail with EADDRINUSE/EACCES, fall
    back to an ephemeral OS-picked port rather than failing outright.
    """
    for offset in range(PORT_BIND_ATTEMPTS):
        candidate = port + offset
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, candidate))
            s.listen(128)
            return s, candidate
        except OSError as exc:
            s.close()
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            continue

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, 0))
    s.listen(128)
    actual = s.getsockname()[1]
    return s, actual


# --- Helpers ---


def _question_exists(session_dir: Path, q_id: str) -> dict | None:
    conn = state.open_session_db(session_dir)
    try:
        row = conn.execute(
            "SELECT q_id, status, source, topic, rationale, confidence, "
            "transcript_window FROM questions WHERE q_id = ?",
            (q_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def _list_questions(session_dir: Path) -> list[dict]:
    conn = state.open_session_db(session_dir)
    try:
        rows = conn.execute(
            "SELECT q_id, status, source, topic, rationale, confidence, "
            "user_notes, in_report FROM questions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _meeting_context_path(session_dir: Path) -> Path:
    return session_dir / "hydra" / "meeting_context.json"


def _research_dir(session_dir: Path) -> Path:
    return session_dir / "hydra" / "research"


def _next_user_edit_number(session_dir: Path, q_id: str) -> int:
    research = _research_dir(session_dir)
    if not research.exists():
        return 1
    prefix = f"{q_id}.user-edit-"
    max_n = 0
    for entry in research.iterdir():
        if (
            entry.is_file()
            and entry.name.startswith(prefix)
            and entry.name.endswith(".md")
        ):
            stem = entry.name[len(prefix) : -len(".md")]
            try:
                n = int(stem)
            except ValueError:
                continue
            max_n = max(max_n, n)
    return max_n + 1


# --- App builder ---


def build_app(hydra_app: HydraApp) -> FastAPI:
    app = FastAPI()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        ctx_path = _meeting_context_path(hydra_app.session_dir)
        phase = state.get_config(
            hydra_app.session_dir, "hydra.phase", default="preflight"
        )
        if not ctx_path.exists():
            phase = "preflight"

        if phase == "live":
            questions = _list_questions(hydra_app.session_dir)
            return templates.TemplateResponse(
                request,
                "live.html",
                {"questions": questions},
            )
        if phase == "review":
            questions = _list_questions(hydra_app.session_dir)
            return templates.TemplateResponse(
                request,
                "review.html",
                {"questions": questions},
            )
        return templates.TemplateResponse(request, "preflight.html", {})

    @app.post("/preflight")
    async def preflight(
        meeting_about: str = Form(...),
        participants: str = Form(""),
        corpus_paths: str = Form(""),
        obsidian_export_dir: str = Form(""),
    ) -> RedirectResponse:
        participants_list = [p.strip() for p in participants.split(",") if p.strip()]
        corpus_list = [
            line.strip() for line in corpus_paths.splitlines() if line.strip()
        ]
        ctx = {
            "meeting_about": meeting_about,
            "participants": participants_list,
            "corpus_paths": corpus_list,
            "obsidian_export_dir": obsidian_export_dir or None,
        }
        hydra_dir = hydra_app.session_dir / "hydra"
        hydra_dir.mkdir(parents=True, exist_ok=True)
        ctx_path = _meeting_context_path(hydra_app.session_dir)
        ctx_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
        hydra_app.meeting_context = meeting_about
        state.set_config(hydra_app.session_dir, "hydra.phase", "live")

        if hydra_app.dispatcher_inst is not None:
            hydra_app.dispatcher_task = asyncio.create_task(
                hydra_app.dispatcher_inst.run()
            )
        if hydra_app.indexer_inst is not None and corpus_list:
            roots = [Path(p) for p in corpus_list]
            hydra_app.indexer_task = asyncio.create_task(
                hydra_app.indexer_inst.index(roots)
            )

        return RedirectResponse(url="/", status_code=303)

    @app.get("/events")
    async def events_stream(request: Request) -> StreamingResponse:
        async def event_generator():
            queue: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
            hydra_app.sse_subscribers.append(queue)
            try:
                initial = {
                    "type": "connected",
                    "ts": datetime.now(UTC).isoformat(),
                }
                yield f"data: {json.dumps(initial)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                if queue in hydra_app.sse_subscribers:
                    hydra_app.sse_subscribers.remove(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/ask")
    async def ask(topic: str = Form("")) -> JSONResponse:
        topic = topic.strip()
        if not topic:
            raise HTTPException(status_code=400, detail="topic required")
        q_id = _next_manual_q_id()
        flag = Flag(
            q_id=q_id,
            topic=topic,
            rationale="user-asked",
            confidence=1.0,
            transcript_window="",
            status="pending",
            source="manual",
        )
        if hydra_app.dispatcher_inst is not None:
            await hydra_app.dispatcher_inst.enqueue_flag(flag)
        await hydra_app.broadcast("question_flagged", {"q_id": q_id, "topic": topic})
        return JSONResponse({"q_id": q_id, "status": "queued"})

    @app.post("/promote/{q_id}")
    async def promote(q_id: str) -> JSONResponse:
        row = _question_exists(hydra_app.session_dir, q_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown q_id")
        if row["status"] != "suggested":
            return JSONResponse(
                {"q_id": q_id, "status": "noop", "current_status": row["status"]}
            )
        state.set_question_status(
            hydra_app.session_dir, q_id=q_id, status="investigating"
        )
        if hydra_app.dispatcher_inst is not None:
            flag = Flag(
                q_id=q_id,
                topic=row["topic"],
                rationale=row["rationale"] or "",
                confidence=float(row["confidence"] or 0.0),
                transcript_window=row["transcript_window"] or "",
                status="pending",
                source=row["source"],
            )
            await hydra_app.dispatcher_inst.enqueue_flag(flag)
        await hydra_app.broadcast("question_promoted", {"q_id": q_id})
        return JSONResponse({"q_id": q_id, "status": "investigating"})

    @app.post("/dismiss/{q_id}")
    async def dismiss(q_id: str) -> JSONResponse:
        row = _question_exists(hydra_app.session_dir, q_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown q_id")
        state.set_question_status(hydra_app.session_dir, q_id=q_id, status="dismissed")
        await hydra_app.broadcast("question_dismissed", {"q_id": q_id})
        return JSONResponse({"q_id": q_id, "status": "dismissed"})

    @app.post("/edit/{q_id}")
    async def edit(q_id: str, notes: str = Form(...)) -> JSONResponse:
        row = _question_exists(hydra_app.session_dir, q_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown q_id")
        research = _research_dir(hydra_app.session_dir)
        research.mkdir(parents=True, exist_ok=True)
        n = _next_user_edit_number(hydra_app.session_dir, q_id)
        artifact = research / f"{q_id}.user-edit-{n}.md"
        ts = datetime.now(UTC).isoformat()
        body = (
            f"# {q_id.upper()} user edit {n}\n\n"
            f"**Edited:** {ts}\n\n"
            f"## Notes\n\n{notes}\n"
        )
        artifact.write_text(body, encoding="utf-8")
        state.set_question_status(
            hydra_app.session_dir,
            q_id=q_id,
            status=row["status"],
            user_notes=notes,
        )
        await hydra_app.broadcast("question_edited", {"q_id": q_id})
        return JSONResponse(
            {"q_id": q_id, "status": "edited", "artifact": str(artifact)}
        )

    @app.post("/finalize")
    async def finalize() -> JSONResponse:
        state.set_config(hydra_app.session_dir, "hydra.phase", "review")
        await hydra_app.broadcast("session_finalizing", {})
        return JSONResponse({"status": "review"})

    @app.post("/export")
    async def export(destination: str = Form(...)) -> JSONResponse:
        report = hydra_app.session_dir / "hydra" / "report.md"
        if not report.exists():
            raise HTTPException(
                status_code=404,
                detail="report.md not found; finalize the session first",
            )
        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report, dest_path)
        state.set_config(
            hydra_app.session_dir, "hydra.last_export_path", str(dest_path)
        )
        await hydra_app.broadcast("session_exported", {"destination": str(dest_path)})
        return JSONResponse({"status": "exported", "destination": str(dest_path)})

    return app
