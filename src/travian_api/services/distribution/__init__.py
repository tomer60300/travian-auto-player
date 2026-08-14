"""Resource distribution planner.

Design and review: ``docs/25-resource-distribution-planner.md``.

Everything in this package is a **pure function of a snapshot**. No module here
performs I/O or touches the game; fetching lives in the existing services and is
passed in. That keeps the whole planner testable without a session and without
spending requests, which is the scarce resource this tool exists to conserve.

Module map (built in dependency order):

    geometry.py    toroidal distance and travel time            [done]
    merchants.py   capacity model + route cost + cycle choice    [done]
    allocation.py  allocation modes -> per-village ship gaps     [done]
    rounding.py    sum-preserving integer cargo                  [done]
    optimizer.py   flows -> routes, budgets, infeasibilities     [first pass]
    schedule.py    the 24-hour beat                              [done]
    planner.py     orchestration: snapshot -> setup sheet        [done]

Not yet built, and deliberately not faked:

* **Hub consolidation** (profile 8.5) and escalation steps 2-3 of 8.4 -- reroute
  via a nearer hub, split cargo across paths. The optimizer sweeps cycles and
  recommends a Trade Office upgrade, then declares infeasibility rather than
  quietly trimming a route to fit.
* **Crop relay through a sub-hub** (profile 3.5). Netting in ``allocation``
  leaves each village either a sender or a receiver of a resource, never both,
  so a relay cannot be expressed. It needs multi-leg flows, not a scheduling
  change.
* **NPC, storage safety, apply and monitoring** (profile sections 6-9).

Nothing in this package hardcodes an account. Village count grows as the account
expands -- 22 today, 23 landing -- and every production figure differs between
runs, so state is always passed in and correctness is pinned by properties that
hold for *any* number of villages rather than by a fixture of one snapshot.

Two review findings are structural rather than incidental, so they are encoded
here rather than left to callers:

* **R1** — the merchant capacity constants are disputed. ``merchants`` keeps
  them in one injectable :class:`~.merchants.MerchantModel` and can derive them
  from observation instead of trusting a default.
* **R5** — a schedule can only be expressed as a repeating daily beat if every
  cycle divides 24 hours, so that is the default cycle set.
"""
