"""Hydra CLI entry point — argparse routing for all verbs."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

from hydra import probe, state, subprocess_runner
from hydra.tailer import TranscriptTailer

DEFAULT_RECORDINGS_ROOT = Path.home() / "recordings"
DEFAULT_PORT = 4125


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


async def _run_start(session_dir: Path) -> None:
    transcript_path = session_dir / "transcript.jsonl"
    tailer = TranscriptTailer(transcript_path, session_dir=session_dir)
    run_task = asyncio.create_task(tailer.run())
    try:
        while not run_task.done():
            try:
                event = await asyncio.wait_for(tailer.queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            print(event, flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        tailer.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(run_task, timeout=2.0)


def cmd_start(args: argparse.Namespace) -> int:
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
        # Fork AFTER the probe so the user sees the confirmation message
        # synchronously; the child inherits the resolved session dir and runs
        # the asyncio loop. asyncio has not yet been started in this process.
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
        asyncio.run(_run_start(result.session_dir))
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
