"""Hydra CLI entry point.

Placeholder for v0.1 scaffold. Full implementation lands in Phase 1
of the plan at ~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Entry point referenced by [project.scripts] hydra in pyproject.toml."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in {"-h", "--help"}:
        print("hydra start [--session PATH] [--corpus PATH...] [--port N]")
        print("hydra finalize <session>")
        print()
        print("Hydra is not yet implemented. See ~/Documents/Projects/.plans/hydra/")
        return 0
    print(f"unrecognized: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
