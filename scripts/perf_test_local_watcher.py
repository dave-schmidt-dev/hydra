"""Phase 2.3 perf-test gate for the local-Gemma watcher option.

Runs Hydra's watcher prompt against a locally-loaded mlx-vlm Gemma model
while Scarecrow's Parakeet transcribes a real (or recorded) audio session
in parallel. Measures whether running both models concurrently degrades
Parakeet's batch latency below the recording-integrity threshold.

PASS CRITERION (per plan PM-1):
    - Zero dropped Parakeet audio batches.
    - Parakeet batch-latency p95 with watcher load <= 1.5x its idle baseline.

USAGE:
    python scripts/perf_test_local_watcher.py \\
        --session ~/recordings/<session-dir> \\
        --gemma-model mlx-community/gemma-3-4b-it-4bit \\
        --duration 300 \\
        --report .cache/perf_test_local_watcher.json

If the pass criterion is met, document the result in HISTORY.md and update
the README to recommend `model = "local-mlx:..."` in config.toml. If not,
the README continues to recommend the cloud Haiku default.

STATUS: skeleton. The full integration with Scarecrow's Parakeet model has
not been verified on hardware in this implementation pass. To complete this
task you need:

1. Scarecrow installed and importable (or invocable via subprocess) so we
   can drive its Parakeet pipeline alongside the watcher.
2. A Scarecrow session with audio that can be replayed (or a live session
   to attach to).
3. The configured mlx-vlm Gemma model already on-disk.

This script will:
    - Refuse to run unless --i-have-read-the-docstring is passed (so it
      cannot be invoked accidentally by CI or pre-commit hooks).
    - Exit 1 with a clear "manual perf-test deferred" message in any
      automated context.

Once the manual run is performed and PASS is confirmed, append the result
to HISTORY.md and (if PASS) update config.example.toml + README to
recommend the local model.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

logger = logging.getLogger("hydra.perf_test_local_watcher")

DEFAULT_GEMMA_MODEL = "mlx-community/gemma-3-4b-it-4bit"
DEFAULT_DURATION_SECONDS = 300.0
DEFAULT_REPORT_PATH = Path(".cache/perf_test_local_watcher.json")
PARAKEET_P95_HEADROOM_MULTIPLIER = 1.5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_test_local_watcher",
        description="Phase 2.3 manual perf-test gate; see module docstring.",
    )
    parser.add_argument(
        "--session",
        type=Path,
        required=True,
        help="Scarecrow session directory to drive Parakeet against.",
    )
    parser.add_argument(
        "--gemma-model",
        type=str,
        default=DEFAULT_GEMMA_MODEL,
        help="mlx-vlm model path or HuggingFace repo id for the watcher.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help="Total perf-test duration in seconds (>=300 recommended).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to write the JSON report.",
    )
    parser.add_argument(
        "--idle-baseline",
        type=float,
        default=None,
        help=(
            "Parakeet batch-latency p95 in seconds with NO watcher load. "
            "Required to compute the pass/fail headroom ratio; measure once "
            "with `--baseline-only` and reuse for subsequent runs."
        ),
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Run Parakeet without the watcher and emit the p95 as the baseline.",
    )
    parser.add_argument(
        "--i-have-read-the-docstring",
        action="store_true",
        help=(
            "Confirm you have read the module docstring and understand "
            "this script needs manual setup to be meaningful."
        ),
    )
    return parser


def _gate_or_exit(args: argparse.Namespace) -> None:
    if not args.i_have_read_the_docstring:
        print(
            "perf_test_local_watcher: manual perf-test deferred.\n"
            "Read the module docstring, then re-run with "
            "--i-have-read-the-docstring once your environment is ready.",
            file=sys.stderr,
        )
        sys.exit(1)


def _attempt_imports() -> tuple[bool, str]:
    try:
        import mlx_vlm  # noqa: F401
    except ImportError as exc:
        return False, f"mlx_vlm not importable: {exc}"
    return True, "mlx_vlm available"


def _run_parakeet_only(args: argparse.Namespace) -> dict:
    raise NotImplementedError(
        "Parakeet baseline mode requires Scarecrow's Parakeet wiring; "
        "see module docstring for the manual-setup steps."
    )


def _run_with_watcher_load(args: argparse.Namespace) -> dict:
    raise NotImplementedError(
        "Concurrent Parakeet + Gemma run requires Scarecrow's Parakeet "
        "wiring and the mlx-vlm Gemma model; see module docstring."
    )


def _summarize(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "n": 0}
    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    return {
        "p50": statistics.median(sorted_latencies),
        "p95": sorted_latencies[max(0, int(n * 0.95) - 1)],
        "p99": sorted_latencies[max(0, int(n * 0.99) - 1)],
        "max": sorted_latencies[-1],
        "n": n,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    _gate_or_exit(args)

    if not args.session.is_dir():
        print(
            f"perf_test_local_watcher: session not found: {args.session}",
            file=sys.stderr,
        )
        return 2

    ok, msg = _attempt_imports()
    logger.info(msg)
    if not ok:
        return 2

    started_at = time.time()
    if args.baseline_only:
        try:
            result = _run_parakeet_only(args)
        except NotImplementedError as exc:
            logger.error("baseline run not yet wired: %s", exc)
            return 2
    else:
        if args.idle_baseline is None:
            print(
                "perf_test_local_watcher: --idle-baseline is required when not "
                "running --baseline-only. Run with --baseline-only first.",
                file=sys.stderr,
            )
            return 2
        try:
            result = _run_with_watcher_load(args)
        except NotImplementedError as exc:
            logger.error("loaded run not yet wired: %s", exc)
            return 2

    report = {
        "script_version": 1,
        "started_at": started_at,
        "duration_seconds": time.time() - started_at,
        "args": {
            "session": str(args.session),
            "gemma_model": args.gemma_model,
            "duration": args.duration,
            "idle_baseline": args.idle_baseline,
            "baseline_only": args.baseline_only,
        },
        "result": result,
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    logger.info("report written to %s", args.report)

    if not args.baseline_only and args.idle_baseline is not None:
        loaded_p95 = result.get("parakeet_latency", {}).get("p95", 0.0)
        threshold = args.idle_baseline * PARAKEET_P95_HEADROOM_MULTIPLIER
        passed = loaded_p95 <= threshold and result.get("dropped_batches", 1) == 0
        print(
            f"perf_test_local_watcher: {'PASS' if passed else 'FAIL'} "
            f"(p95={loaded_p95:.3f}s, threshold={threshold:.3f}s, "
            f"dropped={result.get('dropped_batches', 'unknown')})"
        )
        return 0 if passed else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
