# Hydra

Real-time research agent companion for [Scarecrow](https://github.com/dlschmidt/scarecrow) meeting transcriptions.

While Scarecrow records and transcribes a meeting, Hydra listens to the transcript stream, flags open questions and discussion-worthy topics, dispatches LLM-backed research agents (`claude -p`, `codex exec`, `gemini -p`) to investigate using a local corpus and the internet, and presents draft + refined answers in a local web UI with strict citation discipline. When the session ends, Hydra produces a Markdown report covering questions raised, findings, and suggested directions.

**Status:** v0.1 in development. Core pipeline is wired (state store, tailer, watcher, quota-aware dispatcher, BM25 indexer, FastAPI web UI) and exercised by ~300 tests; the post-session report writer and the `/hydra` Scarecrow integration are the remaining work before tagging.

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
git clone https://github.com/dave-schmidt-dev/hydra.git && cd hydra
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

Or, from a running Scarecrow TUI, type `/hydra` in the notes pane (requires Scarecrow ≥ 1.5.0 with the `/hydra` slash command, shipped as a separate Scarecrow PR).

Or compose a single-command shell alias that launches both:

```bash
alias sch='sc; hydra start --wait-for-session 30 &'
```

This works regardless of how Scarecrow is launched (iTerm dynamic profile, direct alias, or anything else). `hydra start --wait-for-session 30` polls every 500ms for up to 30 seconds for a live Scarecrow session to appear, then attaches. No Scarecrow CLI flag is required — and intentionally not provided, because the iTerm-profile alias doesn't forward CLI arguments.

When Hydra starts, it opens `http://localhost:4125` in your default browser for the pre-flight screen (meeting context, corpus paths). After confirmation, the live view streams flagged questions and answers as the meeting progresses.

## Architecture

High-level:

- **Tailer** — follows Scarecrow's `transcript.jsonl` via `watchdog` with a polling fallback.
- **Watcher** — rolling 30-second window over a cloud Haiku model by default; opt-in local mlx-vlm Gemma after the perf-test gate confirms it doesn't disrupt Scarecrow's audio pipeline.
- **Quota router** — wraps `ai_monitor` via subprocess to pick the provider with the most remaining quota; per-provider 60s blacklist for mid-flight 429s; round-robin fallback when the snapshot is unavailable.
- **Dispatcher + workers** — `claude -p` / `codex exec` / `gemini -p` / `vibe -p` spawned in their own process group with hard timeouts; mid-flight 429 reroutes within a tier; the heavy tier auto-retries once on timeout.
- **Indexer** — SQLite FTS5 over corpus paths (Obsidian vault, recordings, per-meeting attachments) with incremental mtime caching.
- **Web UI** — FastAPI + htmx + SSE on `127.0.0.1:4125` with an ephemeral-port fallback after ten busy candidates.
- **Report writer** — post-session synthesis to `report.md` with full citation preservation; optional copy into a configured Obsidian vault.

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
