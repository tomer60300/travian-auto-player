---
description: Full UX/a11y audit loop on a URL — screenshots at 3 viewports, a11y audit, Core Web Vitals, ux-reviewer critique, then fix and re-run until clean or 3 rounds.
argument-hint: <url> (e.g. http://localhost:5173/login)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, mcp__chrome-devtools__*, mcp__playwright__*
---

Audit target: **$1**

Measure this URL against the **UI Definition of Done** in `frontend/CLAUDE.md`, fix what fails,
and prove the fix. Read that section and the `## Design System` section before you change any CSS.

## Driver

Prefer the **chrome-devtools** MCP server — it is the only one here with `lighthouse_audit` and
real performance traces. If its tools are not present in the session (the plugin was installed
mid-session and its server only attaches after a Claude Code restart), fall back to the
**playwright** MCP server, which is always registered in `.mcp.json`. The mapping is one-to-one
except where noted:

| chrome-devtools | playwright fallback |
| --- | --- |
| `navigate_page` | `browser_navigate` |
| `resize_page` | `browser_resize` |
| `take_screenshot` | `browser_take_screenshot` |
| `take_snapshot` | `browser_snapshot` |
| `list_console_messages` | `browser_console_messages` |
| `evaluate_script` | `browser_evaluate` |
| `press_key` | `browser_press_key` |
| `performance_start_trace` / `_stop_trace` | none — measure LCP/CLS with a `PerformanceObserver` inside `browser_evaluate`, registered with `buffered: true` right after navigation |
| `lighthouse_audit` | none — report the accessibility score as NOT CHECKED rather than faking one |

Say in the final report which driver was used. They are not equivalent, and a reader deciding
whether to trust an accessibility score needs to know Lighthouse never ran.

## Hard rules

- **Localhost only.** Refuse any URL that is not `localhost` or `127.0.0.1`. Never point this at a
  Travian host — there is a live game account on this machine and a single request can burn it.
- **Never touch the server on port 80.** It is the operator's production server. Do not restart,
  stop, or kill it. If the target is `:80`, audit it read-only and make no server changes.
- Frontend dev server is `cd frontend && npm run dev` on port 5173. Start it in the background if
  it is not already up, and stop only an instance you started yourself.
- Fix causes in `frontend/src/**` and `frontend/src/index.css`. Never widen a threshold, delete an
  assertion, or relax a check to make the audit pass. A failure you cannot fix is reported as a
  failure.

## Setup

1. Reject a non-localhost URL now, before doing anything else.
2. Check the target is serving: `curl -s -o /dev/null -w "%{http_code}" $1`. If it is not, and the
   URL is `:5173`, start Vite in the background and wait for it to answer.
3. Create the artifact directory for this run:
   `.claude/ux-audit/<route-slug>/round-1/` — screenshots, `a11y.md`, `console.md`, `cwv.md` go
   there. Later rounds get `round-2/`, `round-3/`. This directory is disposable output; do not
   commit it.

## Round loop — repeat up to 3 times

### 1. Capture

Open the page with `navigate_page`, then for each viewport in **375x812**, **768x1024**,
**1440x900**:

- `resize_page` to that size, `navigate_page` again to the URL so the layout settles from a real
  load rather than a resize reflow, then `take_screenshot` (`fullPage: true`) into the round
  directory as `<width>.png`.
- Before moving on, at 375px only, check for horizontal overflow with `evaluate_script`:
  `document.documentElement.scrollWidth > document.documentElement.clientWidth` and record the
  numbers. A `true` here is an automatic P1.

### 2. Accessibility

- `take_snapshot` for the accessibility tree. Save it to `a11y.md`. This is the reviewer's
  primary evidence — save the whole thing, not a summary.
- `list_console_messages` with `types: ["issue"]` and `includePreservedMessages: true` to pick up
  Chrome's own accessibility issues from page load. Save to `console.md`.
- `lighthouse_audit` with `mode: "navigation"` and `categories` including `accessibility`, writing
  its JSON into the round directory. Do **not** read the whole report — extract only the failures:
  ```
  node -e "const r=require('./report.json'); Object.values(r.audits).filter(a=>a.score!==null&&a.score<1).forEach(a=>console.log(JSON.stringify({id:a.id,title:a.title,items:(a.details&&a.details.items||[]).slice(0,10)})))"
  ```
- Keyboard pass: press Tab repeatedly with `press_key`, reading the focused element each time via
  `evaluate_script` on `document.activeElement` (tag, accessible name, and whether a focus ring is
  computed). Record the order. Screenshot the first three focused states so the reviewer can judge
  indicator visibility. Stop when focus cycles back or after 25 stops, and note if it never cycles
  — that is a keyboard trap.
- Repeat the contrast-sensitive part in **both themes**: toggle
  `document.documentElement.dataset.theme` between `dark` and unset, and capture 1440px in each. A
  token pair that passes in light can fail in dark.

Two traps that have already produced wrong numbers here — do not rediscover them:

- **Never set the theme and read `getComputedStyle` in the same tick.** index.css puts a 200ms
  colour transition on most elements, so a same-tick read returns a half-interpolated colour and
  invents contrast failures that do not exist. Toggle, wait past 200ms, then measure.
- **Fold `opacity` into the colour before computing a ratio.** `.input-field::placeholder` is
  `var(--md-on-surface-variant)` at `opacity: 0.6`; reading `.color` alone reports 7.63 and passes,
  while the real composited value is 2.93 and fails. Blend the declared colour over the element's
  own background at the declared alpha. The same applies to any `rgba()` text colour.
- Measure focus rings after a **real `press_key('Tab')`**. A programmatic `.focus()` does not match
  `:focus-visible` on a button, so it reports the UA default ring instead of the project's.

### 3. Core Web Vitals

`performance_start_trace` with `reload: true` and `autoStop: true`, then read LCP and CLS from the
result. Run `performance_analyze_insight` on the LCP breakdown if LCP is over 2.5s. Save the
numbers to `cwv.md`. Record them as numbers, not adjectives.

### 4. Review

Launch the **ux-reviewer** subagent (`.claude/agents/ux-reviewer.md`). Give it:

- the absolute path to the round directory and the list of files in it,
- the full `a11y.md` contents inline,
- the console issues, the Lighthouse failures, the LCP/CLS numbers, the overflow measurement, and
  the tab-order list.

Do **not** give it source code, the diff, or your own theory of what is wrong. It reviews the
result; that is the whole design. Show its full report to the user verbatim.

### 5. Fix

Take the findings in severity order, P1 first. For each:

- Fix the cause, in tokens. Reach for an existing token or utility class from the `## Design
  System` section before writing a new value; if a token is genuinely missing, add it to **both**
  `:root` and `[data-theme="dark"]` in `frontend/src/index.css`.
- Keep the diff minimal — this is an audit, not a redesign. Do not restyle a component that had
  no finding against it.
- Skip nothing silently. If you decline a finding, say which one and why in the final report.

Then run the frontend gate: `cd frontend && npx eslint . --max-warnings=20 && npm test`.

### 6. Decide

- Reviewer returned **CLEAN** → stop, report.
- Reviewer returned findings and this was round 3 → stop, report the remaining findings as open.
- Otherwise → new round directory, re-capture from scratch, re-review. Re-capture; never reuse the
  previous round's artifacts to claim a fix worked.

## Final report

- Rounds run, and the verdict of each.
- P1/P2/P3 counts per round, so the trend is visible.
- Numbers: LCP and CLS per round, Lighthouse accessibility score per round, 375px overflow yes/no.
- Files changed, one line each.
- Findings still open, with severity and why they were not fixed.
- Anything marked NOT CHECKED or UNVERIFIABLE by the reviewer, and what is needed to check it.

If you started the dev server, stop it. If the run ended with failures, say so plainly — a red
audit reported honestly is the successful outcome here.
