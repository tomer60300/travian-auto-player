# Travian Auto-Player

Full-stack web app. Python 3.12 FastAPI backend + React 19/Vite frontend in plain JavaScript (`.jsx`/`.js`, no TypeScript).
Automates Travian Legends gameplay: farming, scouting, build queues, military, reports.

## Repository Structure
- `src/travian_api/` — FastAPI backend (SQLAlchemy 2.0 async, aiosqlite, Pydantic v2, JWT auth, WebSockets)
- `src/travian_api/web/` — ASGI app, routes, WebSocket handlers, auth, rate limiting
- `src/travian_api/clients/` — HTTP client (httpx + curl_cffi stealth)
- `src/travian_api/parsers/` — HTML/response parsers (BeautifulSoup + lxml)
- `src/travian_api/services/` — Business logic layer
- `src/travian_api/stealth/` — Anti-bot evasion (TLS fingerprinting, request throttling)
- `frontend/` — React 19 + Vite + Zustand 5 + Tailwind CSS v4
- `tests/` — pytest test suite
- `plans/` — YAML build plan files

## Universal Rules
- All commits follow Conventional Commits format
- Never commit secrets. Use environment variables for all credentials.
- Tests required for all new features — run them before declaring done
- Match existing code patterns. Read before writing.
- Minimal diffs only. No unrequested refactors.

## Branches
- Default branch is `main` (renamed from `feature/web-ui`). `travian-developer-platform` no longer exists.
- Name branches after the Conventional Commit type they carry: `feat/<slug>`, `fix/<slug>`,
  `docs/<slug>`, `chore/<slug>`, `refactor/<slug>`.
- CI (`.github/workflows/ci.yml`) runs on `main` only — pushes to `main` and PRs targeting it.

## Dual Server Environment
- Port 80: Stable production server (`--host 0.0.0.0`). NEVER restart without explicit permission.
- Port 8001: Debug/dev server (`--host 0.0.0.0`). Restart freely after Python changes.
- Nothing runs on port 8000 any more. Older docs and prompts that say "production is 8000" are stale.
- Both servers run from this same checkout and serve the same built `src/travian_api/web/static`.
  So `npm run build` ships to production immediately — there is no staging step between debug
  and production, and no server restart is needed for a frontend change to go live.

## Commands
### Backend
- `cd travian-auto-player && uv run fastapi dev` — Dev server
- `cd travian-auto-player && uv run pytest -x -v` — Tests
- `cd travian-auto-player && uv run ruff check . && uv run ruff format .` — Lint + format
- `cd travian-auto-player && uv run uvicorn src.travian_api.web.app:app --port 8001` — Debug server

### Frontend
- `cd travian-auto-player/frontend && npm run dev` — Dev server (Vite, localhost:5173)
- `cd travian-auto-player/frontend && npm run build` — Build to src/travian_api/web/static
- `cd travian-auto-player/frontend && npx eslint .` — Lint

## Code Quality Rules

### Scope Control (CRITICAL)
- Only make changes directly requested or clearly necessary
- A bug fix does not need surrounding code cleaned up
- A simple feature does not need extra configurability
- Do not add docstrings, comments, or type annotations to code you didn't change
- If you create temporary files, delete them when done

### Before Writing Code
- Read existing code in the area you're modifying
- Find similar patterns already in this codebase and align with them
- Check if the logic already exists before writing new code
- Challenge yourself: can this be done by modifying just one existing file?

### Coding Principles
- DRY, KISS, YAGNI. Pure functions preferred. Strict typing everywhere.
- Fix root causes, not symptoms. Raise errors explicitly, never silently ignore.
- No fallbacks unless explicitly requested. No flag parameters. No multi-mode functions.
- When unsure, inspect the codebase instead of inventing patterns.

### Prohibited
- No console.log in production code
- Never delete or skip existing tests
- No dependencies added without clear justification

### Frontend Export Convention
The frontend is plain JavaScript, so there is no `any` rule to enforce. Match the existing
export style instead of imposing a new one:
- Default export for React components, pages, and Zustand stores (plus the axios instance in
  `src/api.js`) — that is all 43 `export default` declarations in `frontend/src` today, and
  `src/App.jsx` imports them that way.
- Named exports for hooks, utilities, and constants (`src/hooks/`, `src/utils/`, `src/constants/`,
  `src/ws.js`, `src/logStream.js`).

## Delivery Workflow (MANDATORY for all features and bug fixes)

Every task MUST follow this exact pipeline. Do not skip steps.

### Phase 1: Design
1. Read the user's request carefully. Ask clarifying questions if ambiguous.
2. Explore the affected code areas — read existing files before proposing changes.
3. Present a brief implementation plan: which files change, what the approach is, any refactors needed.
4. Wait for user approval before writing code (skip if user said "just do it").

### Phase 2: Implement
1. Write the code following all Code Quality Rules below.
2. Keep diffs minimal. Only touch files directly related to the task.
3. Do NOT build the frontend. On this repo a build IS a production deploy: `npm run build`
   writes `src/travian_api/web/static`, which the server on :80 serves directly, and that
   directory is untracked — so there is nothing to revert to. Frontend verification is eslint
   plus vitest (Phase 3 step 5). Deploying is a separate, explicit step the operator authorises.

### Phase 3: Verify

Scope the gate to what you actually changed. Budget a couple of minutes for the
full backend suite in parallel (`-n 8`), not one: measured 2026-09-03 on this
machine, 107s warm to 199s cold across three runs of 1,911 tests, and 99s over
2,031 tests later the same day — against 235–240s serial. Even at two minutes,
running it to verify a Markdown edit verifies nothing and is pure waste.

**Always:**
1. Backend linting, if any Python changed: `uv run ruff check . && uv run ruff format --check .`
2. Show ALL output to the user. Never claim "it works" without evidence.
3. If fixing a bug: write a FAILING test first, then fix, then show it passing.

**Backend (Python) changed:**
4. `uv run --extra dev --extra web pytest -q -n 8 --tb=short`
   Use `--extra dev --extra web`. A bare `uv run pytest` does NOT install the
   optional extras, so it silently falls through to a global pytest whose
   editable install may point at a different checkout — which has already
   produced test results describing the wrong source tree.
   `-n 8` (pytest-xdist) is safe here: every process gets its own tmp DB, tmp
   trace dir, scrubbed env and the live-writes pin, so workers cannot collide.
   While iterating, `-m "not slow"` skips the heavy cases: the oracle
   agreement checks, the relabelling permutations, the mutation guards, and
   every 40-village planner case. Measured 2026-09-03 with `-n 8` over 2,031
   tests: 99s full, 82s skipping slow — the marker buys less than it looks like
   it should, because the slow cases run in parallel with everything else.
   Run the full set (still `-n 8`) before committing.

**Frontend changed:**
5. `cd frontend && npx eslint . --max-warnings=20 && npm test`
   No build: see the Phase 2 note — building deploys to :80.

**Docs, comments or CI YAML only:** none of the above apply beyond a sanity
read. Do not run the test suite.

> The build writes into `src/travian_api/web/static`, which the production
> server on :80 serves directly. There is no staging step: a frontend build is
> live the moment it finishes.

### Phase 4: Submit
1. Stage only the relevant files (never `git add -A`).
2. Commit with Conventional Commits format.
3. Push to the current branch.

### Phase 5: Codex Review Gate
1. After pushing, run `/codex:review --wait` to get a Codex code review.
2. If Codex returns **no P1/P2 issues**: report success to the user. Task is done.
3. If Codex returns **P1 or P2 issues**: fix every issue Codex flagged, then loop back to Phase 3 (verify → commit → push → re-review). Repeat until Codex passes.
4. Always show the Codex review output to the user.

### Evidence-Based Bug Fixing (NON-NEGOTIABLE):
1. Read the actual error/logs FIRST. Do not theorize before reading evidence.
2. Write a failing test that reproduces the bug before attempting any fix.
3. Fix ONLY the broken code. Do not "preemptively" fix working code nearby.
4. After fixing, run the test. Show the output. If it still fails after 2 attempts, STOP and reassess.

## Farm Status Reading
- Empty slots = success
- "Not enough troops" = expected troop exhaustion, not a failure

## Frontend UI Gates

The design tokens and the UI acceptance gate live in `frontend/CLAUDE.md`, not here — they are
frontend-only and would be noise during backend work. Two sections there:

- **## Design System** — the real MD3 tokens in `frontend/src/index.css` (colour, spacing,
  typography, radius, elevation, motion, breakpoints) and the rule that components consume
  tokens only, never raw hex or Tailwind palette colours.
- **## UI Definition of Done** — six conditions every UI change must meet (responsive at
  375/768/1440, keyboard + visible focus, WCAG AA contrast in both themes, 44px tap targets,
  all five states, no CLS and LCP < 2.5s).

Phase 3 for a frontend change is the eslint/vitest/build command above **plus** the UI
Definition of Done. Run `/ux-audit <url>` to measure it against the running app; it drives
Chrome DevTools MCP and hands the artifacts to the `ux-reviewer` subagent
(`.claude/agents/ux-reviewer.md`).
