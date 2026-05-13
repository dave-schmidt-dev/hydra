"""Single-writer JSONL audit log mirroring state.db transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def emit(session_dir: Path, payload: dict) -> None:
    hydra_dir = session_dir / "hydra"
    hydra_dir.mkdir(parents=True, exist_ok=True)
    filtered = {k: v for k, v in payload.items() if v is not None}
    record = {"ts": datetime.now(UTC).isoformat(), **filtered}
    line = json.dumps(record) + "\n"
    path = hydra_dir / "questions.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def tail(session_dir: Path, n: int | None = None) -> list[dict]:
    path = session_dir / "hydra" / "questions.jsonl"
    if not path.exists():
        return []
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if n is not None:
        lines = lines[-n:]
    return [json.loads(line) for line in lines]
