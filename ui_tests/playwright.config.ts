import { defineConfig } from "@playwright/test";

// Phase 6.2: webServer auto-starts hydra with HYDRA_MOCK_CLIS=1 so worker
// subprocesses never shell out to claude/codex/gemini/vibe during e2e.
//
// WHY workers=1 + serial projects: all specs share a single HydraApp +
// session.db inside tests/fixtures/sample_session. The session state machine
// progresses preflight -> live -> review across the four specs; projects'
// `dependencies` enforce that order. pretest wipes the session's hydra/
// subdir before each `pnpm test` so the first project sees a fresh DB and
// hydra's init_session_db is the one that creates the schema.
export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  // Per CLAUDE.md: workers must respect CI. Local runs are also serialized
  // because all specs share a single HydraApp + session.db.
  workers: process.env.CI ? 1 : 1,
  retries: 1,
  reporter: [["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:4127",
    trace: "on-first-retry",
  },
  projects: [
    { name: "preflight", testMatch: /preflight\.spec\.ts/ },
    {
      name: "live_updates",
      testMatch: /live_updates\.spec\.ts/,
      dependencies: ["preflight"],
    },
    {
      name: "post_session_review",
      testMatch: /post_session_review\.spec\.ts/,
      dependencies: ["live_updates"],
    },
    {
      name: "keyboard_shortcuts",
      testMatch: /keyboard_shortcuts\.spec\.ts/,
      dependencies: ["post_session_review"],
    },
  ],
  webServer: {
    command:
      "HYDRA_MOCK_CLIS=1 uv run python -m hydra start --session tests/fixtures/sample_session --port 4127",
    cwd: "..",
    url: "http://127.0.0.1:4127",
    timeout: 30_000,
    // Always start fresh so pretest's state-wipe pairs with a clean
    // init_session_db on the new process.
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
  },
});
