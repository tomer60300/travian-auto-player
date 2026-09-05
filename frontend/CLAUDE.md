# React Frontend Conventions

- Functional components with hooks only. No class components.
- Zustand 5 for UI state only (modals, sidebar, form drafts). Server data via fetch/axios.
- Tailwind CSS v4: uses `@tailwindcss/vite` plugin — NO tailwind.config.ts, NO PostCSS config
- Component files: PascalCase (e.g., `UserCard.jsx`). Hooks: `useAuth.js`
- Plain JavaScript, no TypeScript — `src/` is all `.jsx`/`.js`, so there are no type annotations.
- Default export per component, page, and Zustand store; named exports for hooks, utils, constants.
- No console.log in production code (the Vite build drops `console`/`debugger`).
- API calls: axios instance in `src/api.js`, all endpoints centralized there
- WebSocket: wrapper in `src/ws.js`, real-time log streaming via `src/logStream.js`
- Routing: react-router-dom v7, routes defined in `src/App.jsx`
- Build output: `../src/travian_api/web/static` (served by FastAPI — this is the live production bundle)
- Dev server proxy: `/api` -> localhost:8001, `/ws` -> ws://localhost:8001; override the port with `TRAVIAN_BACKEND_PORT`
- Styles in `src/index.css` with Tailwind utility classes
- ESLint flat config in `eslint.config.js`

## Design System

Single source of truth: `frontend/src/index.css`. It already defines a complete
Material Design 3 token set — do not introduce a second one. Tokens are declared as CSS custom
properties on the `:root` block at the top of the file and re-declared under `[data-theme="dark"]`
immediately below it, then exposed to JSX through the semantic utility classes under the
`── Utility Colors ──` section header.

Cited by SELECTOR and section header, never by line number, and with no line count: the
previous version of this file gave line numbers that the same branch's own CSS edits
invalidated, so every citation pointed a few lines off and the count was 16 short.

There is **no `@theme` block** in index.css, so Tailwind's own palette is not wired to these
tokens. `text-gray-400` and `var(--text-secondary)` are unrelated values and only one of them
flips with the theme. That is the single most important fact in this section.

### Colour tokens

Semantic layer — the only names components should reach for. Light value / dark value:

| Token | Light | Dark | Utility class |
| --- | --- | --- | --- |
| `--bg-base` | `#FFFBFE` | `#141218` | `.bg-base` |
| `--bg-surface` | `#F7F2FA` | `#211F26` | `.bg-surface` |
| `--bg-card` | `#F3EDF7` | `#2B2930` | `.bg-card` |
| `--text-primary` | `#1C1B1F` | `#E6E1E5` | `.text-primary` |
| `--text-secondary` | `#49454F` | `#CAC4D0` | `.text-secondary` |
| `--accent-gold` | `#6750A4` | `#D0BCFF` | `.text-gold` |
| `--accent-gold-hover` | `#7965AF` | `#B69DF8` | — |
| `--success` | `#386A20` | `#A8D88E` | `.text-success` |
| `--danger` | `#BA1A1A` | `#FFB4AB` | `.text-danger` |
| `--warning` | `#7D5700` | `#FFDDB3` | `.text-warning` |
| `--info` | `#0061A4` | `#A8C8FF` | `.text-info` |
| `--border` | `#CAC4D0` | `#49454F` | `.border-default` |

`--accent-gold` is a historical name; the value is MD3 purple. Do not rename it, and do not
write gold-coloured UI on the strength of the name.

MD3 role layer — `--md-primary`, `--md-on-primary`, `--md-primary-container`,
`--md-on-primary-container`, `--md-secondary`, `--md-secondary-container`,
`--md-on-secondary-container`, `--md-tertiary`, `--md-tertiary-container`,
`--md-on-tertiary-container`, `--md-surface`, `--md-surface-container`,
`--md-surface-container-low`, `--md-surface-container-high`, `--md-on-surface`,
`--md-on-surface-variant`, `--md-outline`, `--md-outline-variant`, `--md-error`,
`--md-error-container`, `--md-on-error-container`. Use these when you need an explicit MD3
foreground/background pair; always take the `--md-on-*` token for text on an `--md-*-container`
background, never a `--text-*` token.

Status layer — `--status-running`, `--status-success`, `--status-error`, `--status-waiting`,
`--status-idle`, surfaced as `.status-running` / `.status-success` / `.status-error` /
`.status-waiting` / `.status-idle` (coloured text on a 10% wash of itself).

### Spacing scale

4px base, expressed in rem. Canonical steps:

`0.25rem` 4px · `0.5rem` 8px · `0.75rem` 12px · `1rem` 16px · `1.25rem` 20px · `1.5rem` 24px ·
`2rem` 32px · `3rem` 48px

Existing control padding also uses 2px half-steps (`0.375rem` 6px, `0.625rem` 10px,
`0.875rem` 14px) inside `.btn-*` and `.input-field`. Those are load-bearing for the 44px touch
target maths — leave them alone, but do not add new ones. New layout spacing uses the canonical
4px steps only.

### Typography scale

Family: `'Roboto', system-ui, sans-serif` (the `body` rule). Body `line-height: 1.5`;
`h1, h2, h3` are weight 500, `line-height: 1.3`, `letter-spacing: -0.01em`.

| Step | Size | Use |
| --- | --- | --- |
| xs | `0.75rem` / 12px | badges, chips, table meta |
| sm | `0.8rem` / 13px | dense table cells, secondary labels |
| base | `0.875rem` / 14px | default body and control text (most-used size in the file) |
| md | `1rem` / 16px | emphasised body, `.btn-lg` |
| lg | `1.1rem` / 18px | section headings, nav icons |
| xl | `1.25rem` / 20px | card titles |
| 2xl | `1.5rem` / 24px | page titles |
| 3xl | `2rem` / 32px | logo / hero |

Weights in use: 400 body, 500 headings and buttons, 700 emphasis. `0.7rem`, `0.815rem`,
`0.85rem`, `0.95rem`, `1.15rem` and `1.4rem` also appear as one-offs — these are legacy, not
scale steps. Do not extend them and do not copy them into new components.

Inputs must render at >= 16px on mobile or iOS Safari zooms the viewport on focus; index.css
already pins `font-size: 16px !important` on the mobile input rule for exactly this reason.

### Radius

`8px` small controls · `12px` buttons, inputs, cards (default) · `16px` large cards and sheets ·
`28px` dialogs · `9999px` pills, chips, status dots, scrollbar thumbs · `50%` circular
(spinner, avatar).

`4px`, `6px`, `10px` and `20px` appear as one-offs. Legacy — do not extend.

### Shadow / elevation

Never write a raw `box-shadow`. Use the tokens, which are purple-tinted in light theme and
neutral-black in dark:

- `--elevation-1` + `--elevation-1-border` — resting cards, rows
- `--elevation-2` + `--elevation-2-border` — raised surfaces, primary buttons
- `--elevation-2-hover` — hover/active state of an elevation-2 surface

### Motion

- Easing: `--md-easing: cubic-bezier(0.2, 0, 0, 1)` (its own one-line `:root` rule, just
  below the typography block). The only easing curve.
- `200ms` standard state change (colour, border, background)
- `300ms` large or expressive transitions (size, layout, elevation)
- `150ms` enter animations (`.animate-slide-in`)
- `0.8s linear infinite` spinner · `2s ease-in-out infinite` status pulse
- `@media (prefers-reduced-motion: reduce)`, the last block in the file, already collapses
  every animation
  and transition to `0.01ms` globally. Never add an animation that bypasses it — no JS-driven
  `requestAnimationFrame` tween without a reduced-motion guard.

### Breakpoints

`<= 767px` mobile layout (bottom tab bar, log drawer) ·
`768px-1023px` tablet (sidebar 200px) · `>= 1024px` desktop (sidebar 220px). Plus
`@media (pointer: coarse)` for touch sizing, which is orthogonal to width — do not merge them.

### The rule

**Components consume tokens only. No raw hex, no magic px, no Tailwind palette colours.**

Concretely, in a `.jsx` file under `src/`:

- Use `.text-secondary`, `.bg-card`, `.border-default`, `.btn-primary`, `.status-*` — or
  `style={{ color: 'var(--text-primary)' }}` when no class exists.
- Do **not** use `text-gray-400`, `border-gray-800`, `bg-red-500`. These are fixed values that
  do not flip with `[data-theme]`, so they are theme bugs waiting to be reported.
- Do **not** inline a hex literal. If a colour is missing from the token set, add the token to
  both `:root` and `[data-theme="dark"]` in index.css first, then consume it.

Known outstanding violations as of 2026-09-05 (re-counted; the 640px mobile bucket no longer
exists — `.input-sm` was the last thing in it and now has a base rule) — fix opportunistically when you are already
editing the file, never as a drive-by refactor:

- 60 raw hex literals: `pages/RaidOptimizer.jsx` (27), `components/LogDrawer.jsx` (14),
  `pages/ResourcePlanner.jsx` (13), `components/ResourceBar.jsx` (4),
  `components/MobileNav.jsx` (3)
- 76 Tailwind palette utilities, worst offenders `border-gray-800` (8) and `border-gray-700`
  (9) — both are dark-theme greys that render as near-black hairlines on a light surface.

## UI Definition of Done

A UI change is not done when it renders. It is done when every one of these holds. Check them
against the running app, not against the diff.

1. **Responsive** — renders correctly at 375px, 768px and 1440px wide. No horizontal page
   scroll at 375px; wide tables scroll inside their own container, not the body.
2. **Keyboard** — every interactive element is reachable by Tab, in an order that matches the
   visual order, and operable by Enter/Space. A visible focus state on each one
   (`outline: 2px solid var(--md-primary); outline-offset: 2px`, as `.btn-primary:focus-visible`
   and `.input-field:focus-visible` both do). No `outline: none` without a replacement
   indicator.
3. **Contrast** — meets WCAG AA: 4.5:1 for text under 18.66px/24px, 3:1 for larger text and for
   the non-text boundary of a control. Verify in **both** themes; a token pair that passes in
   light can fail in dark.
4. **Tap targets** — >= 44x44px on coarse-pointer viewports. Table checkboxes and radios are the
   documented exception at 24x24px (WCAG 2.5.8 AA) with padded cells; that exemption does not
   extend to buttons or links.
5. **Five states** — default, loading, empty, error and disabled are all handled and all
   visible. Loading uses `.spinner` or `.skeleton`, never a blank region. Empty says what is
   missing and what to do about it. Error says what failed. Disabled is visibly disabled and not
   focus-trappable.
6. **Stability** — no layout shift as data arrives (reserve the space, or skeleton it), and LCP
   under 2.5s on the main view.

Measure 1-4 and 6 with the `/ux-audit` slash command, which drives Chrome DevTools MCP and
hands screenshots plus the accessibility tree to the `ux-reviewer` subagent. Item 5 is a code
and interaction review; the audit cannot see a state you never render.
