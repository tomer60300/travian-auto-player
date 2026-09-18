# Free reads from the operator's browser

## 1. The idea

A page the operator has already loaded is already in the browser. Reading its DOM costs
**nothing** — no HTTP, no session traffic, nothing the server can observe. The requests
were spent by the human browsing, and reading what they fetched spends them again zero
times.

Every read in this document is therefore free in the only sense that matters here: it
does not touch the Travian account, so it does not consume the daily budget shared with
the farm, oasis and build loops, and it does not need the operator's approval the way a
request does.

## 2. Why it is worth having

The planner offers `Read Trade Office from game (~54 requests)` — 27 villages, two
requests each. That is a large fraction of a day's budget spent on numbers that change
perhaps once a week.

The operator clicking through those same 27 villages spends the requests they were
going to spend anyway. Harvesting the values off those pages costs nothing on top.

**Measured 2026-09-18.** 26 villages read in about two minutes, **0 requests**, against
a stored setup that had been typed by hand:

| village | in game | planner had | drift |
|---|---|---|---|
| 08 | 10 | 6 | **+4** |
| 21 | 12 | 10 | +2 |
| 26 | 11 | 9 | +2 |
| 18 | 14 | 13 | +1 |
| 14 | 8 | 7 | +1 |
| 23 | 13 | 12 | +1 |
| 25 | 12 | 11 | +1 |
| 22 | 11 | 12 | **−1** |

**Eight of 27 were wrong**, and 19 were right. Nothing in the app reports this: a Trade
Office that finishes building silently invalidates a typed number, and the planner goes
on sizing routes against capacity the village no longer has — or, in 22's case, capacity
it never had, since the game reads *lower* than the stored value, which no amount of
building explains. That one is a data-entry error that had been sitting in the setup.

Village 03 is not in the table because it has no Trade Office (stored 0), so there is no
page to read.

## 3. Why the levels matter

`merchants_committed` is driven by carry capacity, and carry capacity is
`base × (1 + 0.2 × trade_office_level)`. A level that is four low makes the planner think
a village needs more merchants per send than it does, which inflates its committed total
against `max_busy_merchants` — the cap whose enforcement is already the subject of
`docs/issues/planner-merchant-cap-not-constraining.md`.

Village **26** is the live example: it blocked a plan by committing 20 merchants against
a budget of 18, and the planner's own hint was that five more Trade Office levels would
fit. Two of those five had already been built and the setup did not know.

## 4. Three traps

Each of these produced a wrong answer before it produced a working one.

**The URL does not change when the village does.** Travian keeps
`build.php?id=29&gid=28` byte-identical across villages whose building occupies the same
slot — 19 and 17 are both `id=29`. A watcher keyed on URL change skips every colliding
village *and says nothing*, so it looks like it is working. Key on page content.

**The DOM is not ready when the URL settles.** The document is swapped after navigation.
A fixed wait is a guess: 1.2s missed three pages out of six. Retry until the element you
need exists.

**Attaching a debugger breaks scrolling on tabs that were already open.** Confirmed by
controlled test — tabs created after the attach scroll normally and survive repeated
connect/disconnect cycles; tabs open at the first attach lose wheel and keyboard
scrolling permanently, while `window.scrollTo()` keeps working. It presents exactly like
a website bug, and was reported as one. Poll the plain `/json/list` HTTP endpoint (which
attaches nothing) and attach only for the instant a DOM read needs it, or give automation
its own Chrome instance.

## 5. Writing values back

The planner's setup lives in **localStorage**, not on the server:
`GET /api/distribution/setup` returns 404 until someone presses *Save setup to server*.

Write through the real inputs rather than into storage — React will not observe a
storage write until a reload, and reloading `/resource-planner` costs about three game
requests, which would undo the point of the exercise. Then verify from localStorage
rather than from the input: an input accepting a value does not prove the app kept it.

## 6. Running it

The procedure, the scripts and the gotchas are the `free-read-open-tabs` skill under
`.claude/skills/`. It covers any building page — `gid=28` is the Trade Office, `gid=17`
the Marketplace — and serves a live view on `127.0.0.1:8777` that fills in as the
operator browses.

One rule when using it: **state the request cost out loud every time.** The operator is
tracking a budget and cannot tell from the outside whether a number arrived free or cost
them.
