# Hydra

Real-time research agent companion for [Scarecrow](https://github.com/dlschmidt/scarecrow) meeting transcriptions.

While Scarecrow records and transcribes a meeting, Hydra listens to the transcript stream, flags open questions and discussion-worthy topics, dispatches LLM-backed research agents (`claude -p`, `codex exec`, `gemini -p`) to investigate using a local corpus and the internet, and presents draft + refined answers in a local web UI with strict citation discipline. When the session ends, Hydra produces a Markdown report covering questions raised, findings, and suggested directions.

**Status:** v0.1 scaffold complete; warp-tier plan reviewed and refined (3 reviewers + pre-mortem). Implementation begins from Phase 1 (state store + tailer + recording-integrity gate).

**Plan:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md`
**Task breakdown:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md`
**Synthesis:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-synthesis.md`

## Requirements

- macOS Apple Silicon (M-series)
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- An active Scarecrow session (Hydra refuses to start otherwise)
- Authenticated CLIs: `claude`, `codex`, `gemini`, `vibe` (pre-flight check verifies availability; missing tools are excluded from the model pool, Hydra still launches with whatever is available)
- [ai_monitor](https://github.com/dlschmidt/ai_monitor) installed on the same machine — invoked via subprocess (`python3 -m ai_monitor --json --once`), NOT imported. Not a Python dependency of this package.
- [Scarecrow](https://github.com/dlschmidt/scarecrow) >= 1.5.0 — Hydra reads its `transcript.jsonl` stream

## Setup

```bash
git clone <repo-url> && cd hydra
uv sync
cp config.example.toml config.toml   # then edit
```

## Usage

Start Scarecrow first, then launch Hydra:

```bash
hydra start                 # auto-attach to active Scarecrow session
hydra start --session PATH  # explicit session
hydra start --port 4125     # web UI port (default 4125)
```

Or, from a running Scarecrow TUI, type `/hydra` in the notes pane (requires Scarecrow ≥ 1.5.0 with the `/hydra` slash-command patch from Phase 9 of the plan).

Or compose a single-command shell alias that launches both:

```bash
alias sch='sc; hydra start --wait-for-session 30 &'
```

This works regardless of how Scarecrow is launched (iTerm dynamic profile, direct alias, or anything else). `hydra start --wait-for-session 30` polls every 500ms for up to 30 seconds for a live Scarecrow session to appear, then attaches. No Scarecrow CLI flag is required — and intentionally not provided, because the iTerm-profile alias doesn't forward CLI arguments.

When Hydra starts, it opens `http://localhost:4125` in your default browser for the pre-flight screen (meeting context, corpus paths). After confirmation, the live view streams flagged questions and answers as the meeting progresses.

## Architecture

See `~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md` for the full design (plans live outside the repo per Dave's plan convention). High-level:

- **Tailer** — follows Scarecrow's `transcript.jsonl`
- **Watcher** — local Gemma reads rolling window, emits flagged questions
- **Quota router** — uses `ai_monitor` to pick the model with the most remaining quota
- **Dispatcher + workers** — subprocess-dispatched `claude -p` / `codex exec` / `gemini -p` for research
- **Indexer** — SQLite FTS5 over corpus paths (Obsidian vault, recordings, per-meeting attachments)
- **Web UI** — FastAPI + htmx + SSE on `127.0.0.1:4125`
- **Report writer** — post-session synthesis to `report.md`

## Development

```bash
uv sync
bash scripts/run_test_suite.sh       # full suite
bash scripts/run_test_suite.sh --fast   # unit + component only
uv run ruff check hydra/ tests/
uv run vulture hydra/ vulture_whitelist.py --min-confidence=80
```

Pre-commit and pre-push hooks enforce lint, dead-code check, and the test suite.

## License

MIT. See `LICENSE`.
