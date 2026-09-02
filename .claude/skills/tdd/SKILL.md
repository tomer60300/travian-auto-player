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
cd travian-auto-player && uv run pytest -x -v tests/
```

### Frontend Tests (when added)
```bash
cd travian-auto-player/frontend && npx vitest run
```
