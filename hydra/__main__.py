"""Hydra CLI entry point — argparse routing for all verbs."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from hydra import probe, state, subprocess_runner

DEFAULT_RECORDINGS_ROOT = Path.home() / "recordings"
DEFAULT_PORT = 4125

logger = logging.getLogger("hydra.cli")


def _maybe_install_mock_cli_hook() -> None:
    # WHY HYDRA_MOCK_CLIS=1 bypasses real LLM CLIs for UI dev + Playwright e2e.
    if os.environ.get("HYDRA_MOCK_CLIS") != "1":
        return

    import time

    from hydra import citations, worker

    async def _mock_run_job(job, *, quota_router, on_event=None, **_kwargs):
        await asyncio.sleep(0.05)
        job.status = "done"
        started = job.started_at if job.started_at is not None else time.monotonic()
        job.started_at = started
        job.completed_at = started + 0.05
        job.duration_s = 0.05
        tier_specs = quota_router.tiers[job.tier]
        model_spec = tier_specs[0]
        job.model_spec = model_spec
        validated = citations.ValidatedAnswer(
            answer=f"Mock answer for {job.flag.topic} [1].",
            citations=[
                citations.Citation(
                    id=1,
                    source_type="web",
                    url="https://mock.example.com",
                    quoted_snippet="mock snippet",
                )
            ],
            unsourced_claims=[],
        )
        job.artifact_path = worker._write_artifact(
            job.session_dir,
            job.q_id,
            validated,
            model_spec,
            job.flag,
        )
        try:
            state.set_question_status(job.session_dir, q_id=job.q_id, status="answered")
        except Exception:
            logger.debug("mock CLI: set_question_status failed", exc_info=True)
        if on_event is not None:
            with contextlib.suppress(Exception):
                await on_event(worker.WorkerEvent(type="job_succeeded", job=job))
        return job

    worker.run_research_job = _mock_run_job


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hydra",
        description=(
            "hydra start [--session PATH] [--corpus PATH...] [--port N]\n"
            "hydra status [--session PATH]\n"
            "hydra stop [--session PATH]\n"
            "hydra report <session-path>\n"
            "hydra finalize <session-path>\n"
            "hydra prune [--before YYYY-MM-DD] [--kill-orphans]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="verb", required=True)

    p_start = subparsers.add_parser("start", help="Attach to a live session")
    p_start.add_argument("--session", type=Path, default=None)
    p_start.add_argument("--corpus", type=Path, action="append", default=None)
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_start.add_argument("--wait-for-session", type=float, default=0.0)
    p_start.add_argument("--background", action="store_true")
    p_start.add_argument("--watcher-model", type=str, default=None)
    p_start.add_argument(
        "--recordings-root", type=Path, default=DEFAULT_RECORDINGS_ROOT
    )

    p_status = subparsers.add_parser("status", help="Print live-session status")
    p_status.add_argument("--session", type=Path, default=None)

    p_stop = subparsers.add_parser("stop", help="Stop the running hydra process")
    p_stop.add_argument("--session", type=Path, default=None)

    p_report = subparsers.add_parser("report", help="Generate the session report")
    p_report.add_argument("session", type=Path)

    p_finalize = subparsers.add_parser("finalize", help="Finalize a session")
    p_finalize.add_argument("session", type=Path)

    p_prune = subparsers.add_parser("prune", help="Prune stale state and orphans")
    p_prune.add_argument("--before", type=str, default=None)
    p_prune.add_argument("--kill-orphans", action="store_true")

    return parser


def _resolve_session(args: argparse.Namespace) -> probe.ProbeResult:
    if args.wait_for_session > 0:
        return probe.find_live_session_blocking(
            args.recordings_root,
            wait_seconds=args.wait_for_session,
            explicit=args.session,
        )
    return probe.find_live_session(args.recordings_root, explicit=args.session)


async def _serve_web(session_dir: Path, port: int) -> None:
    """Wire HydraApp + dispatcher + tailer and serve uvicorn on ``port``.

    Phase 6.2 web-server bootstrap: launches the FastAPI app via ``uvicorn`` so
    the Playwright suite (and humans) can drive the UI. The dispatcher and
    tailer are wired in so the routes are functionally complete; the watcher
    is intentionally NOT started here because it requires a working LLM CLI —
    HYDRA_MOCK_CLIS only stubs ``worker.run_research_job``, not the watcher
    invoker.
    """
    import uvicorn

    from hydra import dispatcher, indexer, quota, tailer
    from hydra.web import HydraApp, build_app

    quota_router = quota.QuotaRouter()
    dispatcher_inst = dispatcher.Dispatcher(
        quota_router=quota_router,
        session_dir=session_dir,
    )
    indexer_inst = indexer.Indexer()
    transcript_path = session_dir / "transcript.jsonl"
    tailer_inst = tailer.TranscriptTailer(transcript_path, session_dir=session_dir)

    hydra_app = HydraApp(
        session_dir=session_dir,
        quota_router=quota_router,
        dispatcher_inst=dispatcher_inst,
        indexer_inst=indexer_inst,
        tailer_inst=tailer_inst,
    )
    app = build_app(hydra_app)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    print(f"hydra web: serving on http://127.0.0.1:{port}/", flush=True)
    try:
        await server.serve()
    finally:
        if hydra_app.dispatcher_task is not None:
            hydra_app.dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hydra_app.dispatcher_task
        if hydra_app.indexer_task is not None:
            hydra_app.indexer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hydra_app.indexer_task


def cmd_start(args: argparse.Namespace) -> int:
    _maybe_install_mock_cli_hook()

    try:
        result = _resolve_session(args)
    except probe.NoLiveSessionError as exc:
        print(f"hydra start: {exc}", file=sys.stderr)
        return 2
    except probe.SessionEndedDuringWaitError as exc:
        print(f"hydra start: {exc}", file=sys.stderr)
        return 2

    print(f"Attached to session: {result.session_dir} ({result.reason})", flush=True)
    state.init_session_db(result.session_dir)

    if args.background:
        pid = os.fork()
        if pid > 0:
            print(f"hydra started in background (pid={pid})", flush=True)
            return 0
        os.setsid()
        with open(os.devnull, "rb") as devnull_in:
            os.dup2(devnull_in.fileno(), 0)
        with open(os.devnull, "wb") as devnull_out:
            os.dup2(devnull_out.fileno(), 1)
            os.dup2(devnull_out.fileno(), 2)

    subprocess_runner.install_handlers_once()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve_web(result.session_dir, args.port))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    del args
    print("hydra status: not yet implemented (Phase 6+)", file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    del args
    print("hydra stop: not yet implemented (Phase 6+)", file=sys.stderr)
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    del args
    print("hydra report: not yet implemented (Phase 6+)", file=sys.stderr)
    return 1


def cmd_finalize(args: argparse.Namespace) -> int:
    del args
    print("hydra finalize: not yet implemented (Phase 6+)", file=sys.stderr)
    return 1


def cmd_prune(args: argparse.Namespace) -> int:
    del args
    print("hydra prune: not yet implemented (Phase 6+)", file=sys.stderr)
    return 1


_DISPATCH = {
    "start": cmd_start,
    "status": cmd_status,
    "stop": cmd_stop,
    "report": cmd_report,
    "finalize": cmd_finalize,
    "prune": cmd_prune,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.verb]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
