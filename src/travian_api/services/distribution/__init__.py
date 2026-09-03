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
* **NPC, storage safety, apply and monitoring** (profile sections 6-9).

Relay is built, in two forms that answer two different questions:

* **Crop relay through a sub-hub** (profile 3.5) is SEARCHED. ``optimizer``'s
  relay move reroutes a crop flow through an intermediate village wherever that
  strictly lowers the objective, and ``schedule`` phases the hub's forward sends
  after its collecting arrivals.
* **A one-hop material relay tier** (profile 5) is DECLARED. Netting in
  ``allocation`` leaves each village either a sender or a receiver of a
  material, so a material relay cannot arise from the flow graph at all and the
  search cannot find one -- which is correct, because section 5 does not ask for
  one to be found: it states that 02 hands its reserved wood to a tier drawn
  from its own neighbour set, and forbids a role village from being in it. So
  the operator names the tier (``VillageConfig.relay_for``) and the planner
  builds its two legs by construction, outside the search. This deliberately
  AMENDS the no-waterfall invariant for materials, to "no material village both
  sends and receives except a declared relay, and no relay feeds a relay"; the
  relay's own warehouse is then checked against the pass-through it has to hold
  (``storage.relay_buffer_findings``).

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
