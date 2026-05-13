# Hydra — Tasks

Status key: pending | in progress | done | blocked

> **Active plan:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md`
> **Active task breakdown:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md`
> **Synthesis:** `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-synthesis.md`

## Rules

- Never overwrite — always append.
- Update status as work progresses.
- Only mark `done` after verification (tests pass, behavior confirmed).
- New sessions get new timestamped sections.
- Keep tasks small and actionable.

## [2026-05-13] — Bootstrap session

### Task 1: Brainstorm + spec
- **Status:** done
- **Description:** Brainstorming session with Claude; six clarifying questions; six design sections approved.
- **Done when:** Spec written to `~/Documents/Projects/.plans/hydra/hydra-2026-05-13.md` ✅

### Task 2: Project scaffold
- **Status:** done
- **Description:** Initial project structure per Dave's conventions.
- **Done when:**
  - `pyproject.toml`, `README.md`, `HISTORY.md`, `TASKS.md`, `LICENSE`, `.gitignore`, `.pre-commit-config.yaml`, `config.example.toml`, `vulture_whitelist.py` present ✅
  - Directory tree (`hydra/`, `tests/`, `scripts/`, `ui_tests/`, `assets/`, `.cache/`) created ✅
  - `uv sync` runs cleanly ✅
  - `tests/test_scaffold.py` — 6/6 passing ✅

### Task 3: Warp-tier review of spec
- **Status:** done
- **Description:** Dispatched contrarian (codex/GPT-5.5), implementation auditor (Vibe/Mistral fallback after Gemini outage), constructive (claude/Opus-4.7) reviewers in parallel. Synthesized findings. Ran fresh-eyes pre-mortem (Kimi K2.5).
- **Done when:**
  - Three reviewer JSON files saved to `~/Documents/Projects/.plans/hydra/` ✅
  - Synthesis written to `hydra-2026-05-13-synthesis.md` ✅
  - Refined plan with Sections 16 + 17 ✅
  - Pre-mortem dispatched and synthesized ✅

### Task 4: GitHub repo
- **Status:** done
- **Description:** Repo at `https://github.com/dave-schmidt-dev/hydra` initialized; initial commit pushed.

### Task 5: Implementation (separate session)
- **Status:** in progress (Phase 1 done; Phase 2 next)
- **Description:** Execute the refined task breakdown at `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md`. Phases 1 through 10. Start with Task 1.1 (state store schema + migrations).
- **Blocked by:** Tasks 1, 2, 3, 4

## [2026-05-13] — Phase 1 implementation session

### Task 1.1: State store + migrations + audit writer
- **Status:** done
- **Done when:** `init_session_db` creates schema + sets `user_version`; v1→v2 migration test passes; BUSY retries verified; circuit-breaker fires on all-retries-exhausted; `questions.jsonl` row-for-row matches `questions` table. ✅

### Task 1.2: Live-session probe
- **Status:** done
- **Done when:** every fixture case (live / post-rotation / abandoned / explicit / no-candidate) returns the correct result; `--wait-for-session` polls every 500ms; aborts cleanly when `session_end` arrives during the wait. ✅

### Task 1.3: Transcript tailer
- **Status:** done
- **Done when:** all tailer tests pass; resume from a crashed run skips events with `elapsed <= last_event_elapsed`. ✅

### Task 1.4: Subprocess runner infrastructure
- **Status:** done
- **Done when:** subprocess cleanup test passes; PGID registry visible via `snapshot()` for diagnostic accessors. ✅

### Task 1.5: Recording-integrity autouse fixture
- **Status:** done
- **Done when:** fixture active across the whole suite from Phase 1 onward; allowlist starts minimal; intentional-violation test catches disallowed writes. ✅

### Task 1.6: CLI verb skeletons
- **Status:** done
- **Done when:** `hydra start --session <fixture>` prints transcript events to stdout; each verb resolves to its handler (stubs return exit 1 + "not yet implemented"); session-end robustness tests pass. ✅

Phase 1 verification: 100 tests pass in 7.16s; ruff/format/vulture clean.

## [2026-05-13] — Phase 2 implementation session

### Task 2.1: Model registry
- **Status:** done
- **Done when:** `MODEL_TIERS` shape matches plan Section 4.3; tier-completeness test passes; config-toml override populates correctly. ✅
- **Side effect:** flipped `config.example.toml` watcher default from `local-gemma` to `claude:claude-haiku-4-5` per PM-1.

### Task 2.2: Watcher loop
- **Status:** done
- **Done when:** dedup tests (auto/suggested bands, char-Jaccard ≥0.5, substring overlap, window expiry) all pass; failure-surfacing tests (immediate banner, 2-in-30s fallback, success resets) all pass. ✅
- **Notes:** model_invoker / on_flag / on_banner / next_q_id / clock are all injected for deterministic tests. Production wiring lands in Phase 4.

### Task 2.3: Local Gemma perf-test
- **Status:** deferred (skeleton committed)
- **Done when:** manual run produces a report.json against a real Scarecrow session and HISTORY.md records PASS or FAIL. ❌ (manual hardware verification required)
- **Reason for deferral:** the script requires Scarecrow's Parakeet pipeline running concurrently with mlx-vlm Gemma. Cannot be verified in this implementation session. The watcher's cloud-Haiku default per PM-1 stays in place until a documented PASS arrives.

Phase 2 verification: 140 tests pass in 11.31s; ruff/format/vulture clean.

## [2026-05-13] — Phase 3 implementation session

### Task 3.1: ai_monitor subprocess wrapper + schema
- **Status:** done
- **Done when:** router picks the expected model per tier across snapshot fixtures; blacklist routes correctly and expires after 60s; all-low banner fires; subprocess failure / schema mismatch fall back to round-robin with single-event emission. ✅
- **Notes:** Pydantic schema modeled against ai_monitor's actual --json output (read from ~/Documents/Projects/ai_monitor); Vibe's usage_percent inverted; capitalized→lowercase provider name mapping.

### Task 3.2: CLI tool availability check
- **Status:** done
- **Done when:** structured per-tool report; missing-tool tier members excludable via `available_set()` → `QuotaRouter(cli_available=...)`; `AllToolsMissingError` surfaces a fatal-but-helpful error before pre-flight finishes. ✅

Phase 3 verification: 173 tests pass in 11.55s; ruff/format/vulture clean.

## [2026-05-13] — Phase 4 implementation session

### Task 4.1: JSON extractor + citation validator
- **Status:** done
- **Done when:** all captured-output fixtures pass (including deeply-nested case per SR-4); citation validator separates cited vs unsourced claims correctly. ✅

### Task 4.2: Dispatcher + workers
- **Status:** done
- **Done when:** mock-driven E2E flow produces q-NNN.md artifacts with valid citation structure; mid-flight 429 reroutes to next-best model without losing the question; quick failures don't cancel deep jobs. ✅
- **Notes:** Implementation had three shallow bugs caught by tests (fire-and-forget event emission, all-unsourced detector, lint nits). Fixed in place rather than dispatching a fix subagent.

Phase 4 verification: 250 tests pass in 12.13s; ruff/format/vulture clean.
