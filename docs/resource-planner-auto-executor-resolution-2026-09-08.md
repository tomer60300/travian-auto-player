# Auto-executor safety review — resolution

Answers `docs/resource-planner-auto-executor-review-2026-09-08.md`. All seven
findings are addressed. The review's own probes asserted the broken behaviour
and were deleted with it; `tests/test_auto_executor_safety.py` asserts the safe
behaviour instead, one class per finding, and every case in it failed before its
fix.

No production deployment, service restart or game action was performed.

## What changed

| ID | Resolution |
| --- | --- |
| AE-01 | The response names the villages a request left work on (`deferred_origins`), and the sweep unions them across chunks. A filtered chunk's zero can no longer erase what an earlier chunk deferred. |
| AE-02 | `stopped_early`, `gold_club_blocked` and `stop_reason` are response fields, serialised on both return paths. The browser halts on them instead of reading `undefined`. |
| AE-03 | A recognised model that cannot be read in full raises `MarketplaceModelInvalid` rather than returning `[]`. Three outcomes now exist where there were two. |
| AE-04 | The initial read checks `ownPlayer.currentVillageId` and every row's `from.id` against the village asked for, before any write decision. |
| AE-05 | A route left carrying stale cargo because the update cap was spent is added to the deferred list, so it counts in `remaining`. |
| AE-06 | The trace remembers a lost write and `_require_live` — which every live write funnels through — refuses the next mutation. |
| AE-07 | The execute lock is keyed by account and event loop, shared by every service instance writing to that account. |

## Decisions worth knowing

**AE-03 refuses more than "unparseable".** A collection whose destination has no
usable id is refused even though the old reader skipped it silently, because
those rows are real routes the reconciler would then not know about — the
failure mode is duplicate creation, not a missing display row. A genuinely empty
marketplace, and a destination with no rows scheduled, both still read as empty:
refusing everything would be safe and useless.

**AE-04 refuses a model that will not say which village it is.** The real
Europe 2 page states `currentVillageId`, so silence means something is wrong
with the page rather than with the expectation. Per-row `from.id` is checked
only when present: the read-back query does not select it, so absent means "not
stated", not "wrong".

Three test fixtures built marketplace pages without that field. They were made
to state it rather than the check being relaxed — a fixture describing a page
the game never serves is how this survived.

**AE-06 does not raise from the trace.** A flush that fails *after* the game
accepted a create must not be reported as a create that did not happen. The loss
is remembered where it occurs and refused one level up, before the next
mutation. The refusal says plainly that rows already written are real and were
not rolled back.

**AE-07 closes the in-process half only.** Two service objects for one account
now share a lock. Two *processes* — :80 and :8001 — still do not, because the
lock lives in memory. That needs an account-scoped durable lease with ownership
and fencing, which is a design change rather than a fix. Until then: one live
executor per account, and treat a second process as unsafe.

**AE-01's count and the sweep's decision are different questions.** The figure
the operator reads ("N create(s) deferred") stays a count of creates; whether to
keep going is decided by the union of villages still owing work. An early
attempt used the village count for both and a Playwright spec caught it, which
is the review's own point about testing through the real response shape rather
than a mock with invented fields.

## A regression the fixes introduced, and caught

Making the read-back's reader strict (AE-03) without translating its error at
the service boundary was a new bug, found by re-reading the callers rather than
by a test: `MarketplaceModelInvalid` is a `ValueError`, every executor path
catches `TravianError`, and `confirm_routes` handled only `None`. A
partly-readable read-back would have escaped as an unhandled 500 -- and the
read-back runs AFTER a write, which is the one moment a crash costs the most.

Translated at the boundary, with `TestTheRefusalReachesCallersAsTheErrorTheyHandle`
pinning both directions. The lesson generalises: strictness belongs in the
parser, one error vocabulary belongs at the service.

A second was caught by a Playwright spec rather than by review -- see the AE-01
note above on the count of creates versus the count of villages.

## Second pass, after independent verification

An independent check of the fixes found four things still wrong. All four
reproduced, and all four are fixed here.

**Nested malformed rows still read as an empty destination.** The first pass
validated the collection and each row but walked the rows container with
`... or []`, so `routes: null`, `routes: {}` and `routes: ""` still meant "no
routes at this destination" — AE-03's own failure mode, one level deeper than
the fix reached. `routes: 5` and a non-numeric cargo escaped as a bare
TypeError/ValueError, past the translation added for exactly this. The container
is now type-checked and every numeric conversion is caught, so the reader
answers with rows, None, or `MarketplaceModelInvalid` and nothing else.

**Budget exhaustion at preflight reported `stopped_early=false`.** The earliest
exit there is, and it gave the one answer that tells an automatic sweep to carry
on. Nothing had been written, but the field is about whether the run finished
its work, not about whether it managed to do damage.

**A cargo-only chunk never asked for a next one.** Continuation was gated on
CREATE attempts, so a sweep that corrected one route and deferred another got a
null wait: the work was remembered and never requested again. An update now
counts as progress.

**A filtered chunk's null wait ended a sweep that still had work.** The server
decides the wait from the request it answered, and once the sweep narrows to one
village that request knows nothing about the others. The client now fills the
gap at the server's own floor (45s) when it still holds unfinished work — never
faster than the server would have asked for, and the stall guard still stops it
looping if the work never clears.

## Third pass, after a second independent verification

Three more gaps, all reproduced, all fixed.

**The stall guard stopped valid unfinished work.** It compared aggregate counts
across differently filtered chunks: village A holding one deferred route and the
next chunk finishing village B both read as "1", so the sweep called it a stall
and stopped before ever going back for A. Two different villages are not two
failed attempts at the same work. It now measures progress on **the origins the
request actually asked for** — no progress means every one of them is still
owed. `plannerReviewSafety.pw.js` carries the reproduction; run against the old
guard it stops after two chunks instead of three, which is how it was confirmed
to catch the defect rather than merely pass.

**Malformed cargo still became invented zeros.** `carriedResources` went through
`... or {}`, so `null`, `[]` and an absent key all produced an all-zero cargo and
handed it back as read inventory. Cargo decides whether a route has drifted, so a
fabricated zero either rewrites a correct route or hides a wrong one. Those are
refused now, along with `{}` — an object naming none of the four resources tells
us nothing, and nothing must not read as nought.

A partial object is deliberately **accepted**, with the unnamed resources read as
zero. The real model states all four every time, but refusing a partial one bets
the whole executor on that holding for every route in every state, and the cost
of being wrong is that every read fails. Stating some amounts is information;
stating none is not.

**Cross-process locking is now enforced, not just documented.**
`services/account_lease.py` holds an account-scoped lease as a file whose
creation is atomic (`O_CREAT | O_EXCL`), so the exclusion survives the process
boundary that `asyncio.Lock` cannot. `/execute` takes it before its in-process
lock and releases it in the same `finally` that unregisters the operation; a
second process is refused with a 409 naming the holder's pid, host and age.

Staleness is by AGE (15 minutes), not by asking whether the holder is alive:
that check would be `os.kill(pid, 0)`, and on Windows that calls
`TerminateProcess` — it would kill the run it was asking about. A lease only
outlives its holder after a hard kill, and then the wait is bounded rather than
permanent. This assumes one filesystem, which is what "the operator's machine"
means here; two machines against one account remain outside its reach.

## Fourth pass — the lease was wrong twice, and the stall guard once more

**The timed lease was the wrong mechanism.** Verification reproduced two defects
in it, both mine. A lease another process may take after 15 minutes assumes no
legitimate run lasts longer, and the browser's 180-second request timeout does
not stop the backend, so a second executor could start while the first was still
writing. And release was an unconditional `unlink`: a holder finishing late
deleted the REPLACEMENT'S lease and admitted a third.

Replaced with an exclusive byte-range lock held by the **operating system**
(`fcntl.flock` / `msvcrt.locking`, non-blocking). It cannot be taken from a live
holder, it cannot be released by anyone else, and the kernel drops it when the
handle closes — including on a kill or a power cut. There is no TTL, no
staleness rule and nothing to clean up; the lock file is never deleted, because
deleting it is what caused the second defect.

Verified beyond unit tests: a subprocess holding the lock refuses this one and
the message names it, and killing that subprocess frees the lock immediately.

**The stall guard was still too blunt.** A village needing three routes and
capped at one per chunk stays pending after chunk one and after chunk two — with
a route created each time — and the guard read that as no progress and stopped a
chunk short. "Still owed" is not "getting nowhere". It now also requires that
the chunk wrote **nothing**: no create, no unverified create, no update, disable
or re-enable. Both directions are pinned in `plannerReviewSafety.pw.js` — the
three-chunk case, and a blocked account that writes nothing twice and must still
stop rather than loop.

Each of those two specs was run against the previous code first and fails there,
so they catch the defects rather than merely passing.

## Not addressed here

The review's "additional verification still needed" list is open coverage, not
reproduced defects: navigation away mid-sweep, editing safety controls during a
sweep, account replacement mid-request, process termination between write and
readback, HTTP timeout followed by retry, and realistic malformed game
responses. None is fixed by this change and none is claimed to be.

The release gate the review sets out still stands. These fixes make the reported
failures fail closed; they do not by themselves certify unattended execution.

## Verification

- `ruff check .` and `ruff format --check .` clean.
- Full backend suite: **3,895 passed, 6 skipped** (`-n 8`), including 55 cases
  in `tests/test_auto_executor_safety.py` — one class per finding, plus the
  regression above and the second-, third- and fourth-pass findings.
- Frontend: eslint 0 errors (16 pre-existing warnings), **678 vitest passed**.
- Playwright, the specs that drive the sweep and the planner:
  `plannerReviewSafety`, `liveRunGuards`, `func-planner-run`, `wholeDayReview`,
  `prunePersistence`, `merchantMeasured`, `problemLines`, `setupStore` — all
  passing. The **entire** Playwright suite, both projects: **543 passed**.

Every new case was confirmed to fail before its fix. Callers of all three
changed readers were audited by hand, which is how the regression above was
found.
