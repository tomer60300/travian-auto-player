"""Resource distribution planner.

Design and review: ``docs/25-resource-distribution-planner.md``.

Everything in this package is a **pure function of a snapshot**. No module here
performs I/O or touches the game; fetching lives in the existing services and is
passed in. That keeps the whole planner testable without a session and without
spending requests, which is the scarce resource this tool exists to conserve.

Module map, in dependency order -- each imports only from the ones above it.
These eleven are the chain :func:`.planner.build_plan` walks:

    findings.py       one fact, what it costs, what to do about it
    geometry.py       toroidal distance and travel time
    merchants.py      capacity model + route cost + cycle choice
    rounding.py       sum-preserving integer cargo
    allocation.py     allocation modes -> per-village ship gaps
    roles.py          what a village is FOR, and what that permits
    npc.py            section 7: what the operator's gold can fund
    optimizer.py      flows -> routes, budgets, infeasibilities
    night_profile.py  the overnight allocations, derived not declared
    schedule.py       the 24-hour beat
    planner.py        orchestration: snapshot -> setup sheet

Six more the web layer calls *around* that chain and never from inside it, so a
plan built by calling ``build_plan`` directly carries none of their findings:

    storage.py        will a village overflow its warehouse, or starve?
    export.py         the confirmed plan as YAML the operator can diff
    execution_trace.py  a verbatim record of what a live run decided and sent
    run_history.py    a zero-request audit of past ``/execute`` runs
    route_revert.py   how to put a village's routes back the way they were
    window_pruning.py which rows of a fanned-out route fall outside the hours

There is deliberately no status column. Everything listed is built and reached
by something; the old map had one, and it still marked ``optimizer`` a first
pass across 2,872 lines, the escalation ladder and the declared relay tier. A
column that records how far the build had got on the day someone typed it rots
whatever it says, where the dependency order above stays true as long as the
imports do.

The escalation ladder of profile 8.4 is built end to end: sweep other cycles,
reroute via a nearer hub (that is relay, below), split the cargo across several
senders -- which the seed's residual augmenting paths do by construction --
recommend a Trade Office upgrade (``optimizer._trade_office_levels_needed``
names itself step 4), then declare the village infeasible. That last rung is a
decision rather than a gap: the optimizer declares infeasibility rather than
quietly trimming a route to fit.

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
