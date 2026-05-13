# Hydra — History

## 2026-05-13

### Project bootstrap

- Brainstorming session: six design decisions pinned (surface = local FastAPI web app; standalone process; watcher + quick + deep three-phase research; configurable corpus with non-blocking FTS5 index; multi-signal live-session probe; strict citation discipline).
- Spec written: `~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md`.
- Project scaffold created: `pyproject.toml`, `README.md`, `HISTORY.md`, `TASKS.md`, `LICENSE` (MIT), `.gitignore`, `.pre-commit-config.yaml`, `config.example.toml`, `vulture_whitelist.py`, `hydra/`, `tests/`, `scripts/`, `ui_tests/`, `assets/`, `.cache/`.
- `scripts/run_test_suite.sh` + `hydra/__main__.py` placeholder; smoke tests passing (`tests/test_scaffold.py`, 6/6).

### Warp-tier plan review

Per `~/.agent/prompts/plan.md` warp tier protocol:
- **Contrarian reviewer** (codex / GPT-5.5): 10 findings (4 high, 6 medium). Read Scarecrow + ai_monitor source trees and verified every plan claim against real file:line evidence. Caught: `ai_monitor.providers.fetch_provider_snapshot()` API misdescription (CR-1); `ai_monitor` provider credential writes violate Hydra's allowlist if imported (CR-2); live-session probe broken after 60-min segment rotation (CR-3); `/hydra` slash command placement wrong (CR-7).
- **Implementation auditor** (gemini / 3.1 Pro): **failed (skipped)** — Google Cloud Code API returned "No capacity available for model gemini-3.1-pro-preview" across both attempts. Plan.md rule: do not block on one reviewer.
- **Implementation auditor fallback** (vibe / Mistral, per user direction): 0 findings; verified scaffold structure but didn't read external source trees.
- **Constructive reviewer** (claude / Opus 4.7): 13 findings (4 high, 6 medium, 3 low). Process-quality wins: move recording-integrity test to Phase 1 as autouse fixture (CV-1); state.db needs `PRAGMA user_version` + migrations (CV-2); resume cursor was referencing a field not in schema (CV-3); subprocess process-group cleanup missing (CV-4); model identifiers needed a single source of truth (CV-5).
- **Fresh-eyes pre-mortem** (cursor-agent / Kimi K2.5): 9 failure modes + 4 systemic risks + 8 gaps. Highlights: MLX GPU contention between Hydra watcher and Scarecrow Parakeet (PM-1); SQLite WAL all-retries-exhausted behavior unspecified (PM-4); resume cursor timestamp was clock-skew-vulnerable (PM-5); ai_monitor JSON format drift (PM-7); port binding race (PM-9).

Final synthesis: **21 ACCEPT / 1 ACKNOWLEDGE / 0 REJECT / 1 ESCALATE-resolved** across 23 reviewer findings + **62% MITIGATE / 38% ACKNOWLEDGE / 0% ESCALATE** across 13 pre-mortem items. Calibration within healthy band.

Material design changes from review:
- ai_monitor invoked via subprocess only (never imported) — preserves recording-integrity invariant by construction (CR-2)
- Default watcher switched from local Gemma to `claude haiku 4.5` (zero GPU contention by default); local Gemma opt-in after Phase 2 perf test (PM-1)
- Live-session probe is multi-signal (no `session_end` AND (audio.wav OR audio_seg*.wav OR recent jsonl mtime)) — survives 60min segment rotation (CR-3)
- Resume cursor uses Scarecrow's `elapsed` field, not wall-clock timestamp — immune to NTP / sleep-wake / clock skew (PM-5)
- Recording-integrity test is now a Phase 1 pytest autouse fixture, not a Phase 8 add-on (CV-1)
- `state.db` gets `PRAGMA user_version` + migrations module; SQLite `BUSY` retry/backoff with 30s circuit breaker (CV-2 + PM-4)
- Subprocess cleanup via `process_group=0` + PGID registry + signal/atexit handlers (CV-4 + PM-6 partial)
- New CLI verbs: `hydra status`/`stop`/`report`/`finalize`/`prune` (with `--kill-orphans`); `--wait-for-session SECS` and `--background` flags on `start` (CV-9 + ESCALATE-1 + PM-6)
- Scarecrow patch reduced to `/hydra` slash command only (no CLI flag); ~20 LOC; in command-interception path not note-prefix path (CR-7 + ESCALATE-1)
- Ephemeral port fallback after 10 sequential bind attempts (PM-9)
- New `cli_check.py` pre-flight verification of `claude`/`codex`/`gemini`/`vibe` availability (SR-3)
- New `ai_monitor_schema.py` Pydantic model for parsed ai_monitor output (PM-7)
- Phase 8.5 cross-project contract test asserts Hydra's known-events set against Scarecrow's README event table (SR-2)
- ai_monitor dropped as a Python dependency; subprocess invocation only

Plan refined inline; new Sections 16 (reviewer-finding refinements) and 17 (pre-mortem refinements) document every change. Per-plan task breakdown at `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md`.

### GitHub repo

Repo created at `https://github.com/dave-schmidt-dev/hydra` (public). Initial commit pushed with full scaffold; planning artifacts intentionally NOT committed to repo (they live under `~/Documents/Projects/.plans/hydra/` per Dave's plan convention).

### Phase 1 implementation

Strategic-orchestrator pass executing `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md` Phase 1 via parallel subagents (Wave 1: Tasks 1.1+1.2+1.4 in parallel; Wave 2: 1.3+1.5 in parallel; Wave 3: 1.6). All six Phase 1 tasks complete with 100 tests green, ruff/format/vulture all clean.

- **Task 1.1** — `hydra/state.py` (WAL + busy-retry decorator with 5-attempt exponential backoff and 30-second circuit breaker), `hydra/migrations.py` (numbered, transactional migrations; split-and-execute preserves outer ROLLBACK), `hydra/audit.py` (single-writer JSONL mirror of state transitions, None-field-filtered).
- **Task 1.2** — `hydra/probe.py` multi-signal live-session probe: no `session_end` AND (audio.wav OR audio_seg*.wav OR transcript.jsonl mtime within 5 minutes). Tail-64KB substring scan for `"session_end"` is robust to truncated final lines. `find_live_session_blocking` polls for `--wait-for-session SECS` with injectable clock/sleep for deterministic tests.
- **Task 1.3** — `hydra/tailer.py` watchdog + polling-fallback transcript follower; bounded `asyncio.Queue` with drop-oldest semantics; single binary file handle (no reopen on `segment_boundary`); resume cursor via Scarecrow's monotonic `elapsed` field with dual-threshold persistence (5s OR 100 events) plus final force-flush on stop. Added `state.set_config`/`state.get_config` helpers.
- **Task 1.4** — `hydra/subprocess_runner.py` process-group-aware async spawner (`process_group=0`), in-memory PGID registry, SIGINT/SIGTERM/atexit cleanup walking `killpg(pgid, SIGTERM)` → 2s grace → `SIGKILL`. Test verifies child lives in its own pgroup distinct from Hydra's, so cleanup cannot take down the parent.
- **Task 1.5** — `tests/conftest.py` autouse recording-integrity fixture. Patches `builtins.open`/`Path.write_text`/`Path.write_bytes`/`os.fsync`. Caller-frame discrimination via `sys._getframe` walk: writes are policed only when the calling stack contains a frame under `<repo>/hydra/`; pytest, sqlite, watchdog, site-packages all pass through. Opt-out marker `@pytest.mark.allow_writes_anywhere` registered in `pyproject.toml`. Fork+exec limitation documented (ai_monitor subprocess writes correctly invisible by construction — PM-2).
- **Task 1.6** — `hydra/__main__.py` argparse routing for `start`/`status`/`stop`/`report`/`finalize`/`prune`. `start` resolves the session via probe → initializes state.db → runs `TranscriptTailer` printing events to stdout (Phase 1 smoke test). `--wait-for-session SECS` and `--background` flags wired; background path forks AFTER probe so the user sees attach confirmation synchronously before the parent returns. Other verbs are stubs returning exit code 1 with "not yet implemented (Phase 6+)".

Test inventory: scaffold (6), state schema (6), migrations (5), audit writer (8), state config (6), session probe (15), session-end robustness (8), subprocess cleanup (12), tailer (9), recording-integrity fixture (8), CLI verbs (17) = **100 passing in 7.16s**.
