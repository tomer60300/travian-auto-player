---
name: tdd
description: Test-Driven Development with isolated Red-Green-Refactor cycle
---

## TDD Workflow

Follow the strict Red-Green-Refactor cycle for every feature or bug fix.

### Phase 1: RED (Write a failing test)

1. Understand the requirement or bug completely before writing any code
2. Write ONE test that captures the expected behavior
3. **RUN the test** and confirm it FAILS with the expected failure
4. Show the failing test output — do NOT proceed without it
5. If the test passes immediately, the test is wrong or the feature already exists

**For bug fixes:** The failing test MUST reproduce the actual bug. Read error logs first.

### Phase 2: GREEN (Minimal implementation)

1. Write the MINIMUM code to make the failing test pass
2. Do not add extra features, edge cases, or optimizations
3. **RUN the test** and confirm it PASSES
4. Show the passing test output — do NOT proceed without it
5. Also run the full test suite to check for regressions

**Rules:**
- No refactoring during GREEN phase
- No additional tests during GREEN phase
- Only make the ONE failing test pass

### Phase 3: REFACTOR (Clean up)

1. Clean up the implementation (remove duplication, improve names, simplify)
2. Clean up the test if needed
3. **RUN the full test suite** — all tests must still pass
4. Show the test output — do NOT skip this

### Cycle Rules

- One test at a time. Never write multiple failing tests.
- Each phase must show actual test output as evidence.
- If GREEN takes more than 2 attempts, STOP and reassess the approach.
- If a refactor breaks tests, revert the refactor immediately.
- Keep the cycle tight: each Red-Green-Refactor should be under 15 minutes.

### Backend Tests
```bash
cd travian-auto-player && uv run --extra dev --extra web pytest -q -n 8 --tb=short
```

`--extra dev --extra web` is not optional: a bare `uv run pytest` does not
install the extras and falls through to a global pytest whose editable install
may point at a different checkout, which has already produced results
describing the wrong source tree. `-n 8` is safe here (per-worker tmp DB, tmp
trace dir, scrubbed env). Measured 2026-09-03 over 1,911 tests: serial 240s,
`-n 8` between 107s warm and 199s cold -- budget a couple of minutes, not one.
While iterating inside one Red-Green cycle, add `-m "not slow"`; run the full
set before committing.

### Frontend Tests
```bash
cd travian-auto-player/frontend && npx eslint . --max-warnings=20 && npm test && npm run build
```

`npm test` is vitest (`vitest run`). Note that `npm run build` writes into
`src/travian_api/web/static`, which the production server on :80 serves
directly -- there is no staging step, so a build is a deploy. Leave it out of
a Red-Green cycle and run it once at the end.
