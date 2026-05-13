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

## [2026-05-13] — Phase 5 implementation session

### Task 5.1: BM25 indexer
- **Status:** done
- **Done when:** small fixture corpus → queryable index; second run with one touched file re-indexes only that file; all skip rules verified (.git, node_modules, .venv, __pycache__, .obsidian, .DS_Store, plus .pytest_cache/.ruff_cache/.mypy_cache defensive additions). ✅

Phase 5 verification: 274 tests pass in 12.20s; ruff/format/vulture clean.

## [2026-05-13] — Phase 6 implementation session

### Task 6.1: FastAPI app + templates
- **Status:** done
- **Done when:** full pre-flight → live → review flow runs against stubbed dispatcher/indexer; ephemeral-port fallback (PM-9) verified after 10 sequential bind attempts. ✅

### Task 6.2: Playwright UI suite + config
- **Status:** done
- **Done when:** Playwright HTML report renders; all four spec files pass against the mocked workers. ✅
- **Notes:** Subagent hit pnpm/npm sandbox restriction; stepped in directly to run pnpm install + playwright install chromium + the suite. 6/6 specs passing.
- **Integration:** `_serve_web()` now lives in `hydra/__main__.py` and wires the full stack (QuotaRouter + Dispatcher + Indexer + TranscriptTailer + uvicorn). `HYDRA_MOCK_CLIS=1` monkey-patches `worker.run_research_job` for UI dev.

Phase 6 verification: 303 Python tests + 6 Playwright specs all green; ruff/format/vulture clean.

## [2026-05-13] — Phase 7 implementation session

### Task 7.1: Report synthesis
- **Status:** done
- **Done when:** fixture session with N answered questions produces a report.md with all expected sections + preserved citations; pruning (in_report=0) excludes questions from the synthesis prompt and output. ✅
- **Notes:** writes both report.md and report.generated.md; failure modes (timeout/nonzero/empty) preserve report.generated.md and write a placeholder report.md. Obsidian export with YAML frontmatter when configured.

Phase 7 verification: 319 tests pass in 12.60s; ruff/format/vulture clean.

## [2026-05-13] — Phase 8 implementation session

### Task 8.1: Synthetic e2e session
- **Status:** done
- **Done when:** e2e test passes deterministically (verified across 5 re-runs); recording-integrity fixture stays green across the whole suite. ✅

### Task 8.2: Opt-in real-CLI integration
- **Status:** done
- **Done when:** test passes manually at least once; failure modes documented. ✅
- **Real-CLI result:** all 9 tests pass against live claude / codex / gemini / vibe on this machine; JSON extraction works against every CLI's actual stdout.

### Task 8.3: Manual smoke test on real Scarecrow session
- **Status:** deferred (manual hardware verification required)
- **Done when:** manual run produces a coherent report.md; observed issues filed in HISTORY.md follow-ups.
- **Reason for deferral:** requires a real or saved Scarecrow recording. To be run before tagging v0.1.

### Task 8.5.1: Scarecrow event-type contract test
- **Status:** done
- **Done when:** contract test passes; README minimum-version note (`scarecrow >= 1.5.0`) already present. ✅
- **Drift status:** perfect alignment — 13 documented events, 13 known events.

Phase 8 verification: 324 fast tests + 2 e2e + 9 integration + 5 Scarecrow-contract all green; ruff/format/vulture clean.

## [2026-05-13] — Phase 9 + Phase 10 closure

### Phase 9.1: Scarecrow /hydra slash command PR
- **Status:** deferred (separate Scarecrow-repo PR)
- **Reason:** changes `~/Documents/Projects/Scarecrow/` and ships as a Scarecrow release; not in this repo's scope. Plan Section 8 captures the patch shape.

### Phase 9.2: Cross-project documentation
- **Status:** done on the Hydra side (README already documents the alias pattern + slash command)
- **Reason:** Scarecrow-side README updates land with the Phase 9.1 PR.

### Phase 10.1: Real-CLI regression sweep
- **Status:** done (covered by Phase 8.2's opt-in integration suite — 9/9 pass).

### Phase 10.2: Performance profiling
- **Status:** deferred (manual hardware verification required)
- **Reason:** needs real workloads (large Obsidian vault, 20-question SSE burst, sustained watcher load). To run before tagging v0.1.

### Phase 10.3: Final documentation pass
- **Status:** done (README scrubbed of internal plan paths and stale watcher description; HISTORY has per-phase entries; TASKS lists every task with explicit status).

## v0.1 tagging gate

The four explicitly-deferred items below must be completed before tagging:
- **Task 2.3** — local-Gemma perf-test against a real Scarecrow + Parakeet + mlx-vlm load. Script is in place.
- **Task 8.3** — manual smoke test on a real or saved Scarecrow recording.
- **Task 9.1** — `/hydra` slash command PR to the Scarecrow repo.
- **Task 10.2** — performance profiling sweep.
