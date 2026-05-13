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
- **Status:** pending
- **Description:** Execute the refined task breakdown at `~/Documents/Projects/.plans/hydra/hydra-2026-05-13-tasks.md`. Phases 1 through 10. Start with Task 1.1 (state store schema + migrations).
- **Blocked by:** Tasks 1, 2, 3, 4
