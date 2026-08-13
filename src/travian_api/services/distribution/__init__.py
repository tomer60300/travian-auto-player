"""Resource distribution planner.

Design and review: ``docs/25-resource-distribution-planner.md``.

Everything in this package is a **pure function of a snapshot**. No module here
performs I/O or touches the game; fetching lives in the existing services and is
passed in. That keeps the whole planner testable without a session and without
spending requests, which is the scarce resource this tool exists to conserve.

Module map (built in dependency order):

    geometry.py    toroidal distance and travel time            [done]
    merchants.py   capacity model + route cost + cycle choice    [done]
    allocation.py  allocation modes -> per-village ship gaps     [next]
    optimizer.py   hub assignment and route selection
    schedule.py    the 24-hour beat
    planner.py     orchestration: snapshot -> Plan

Two review findings are structural rather than incidental, so they are encoded
here rather than left to callers:

* **R1** — the merchant capacity constants are disputed. ``merchants`` keeps
  them in one injectable :class:`~.merchants.MerchantModel` and can derive them
  from observation instead of trusting a default.
* **R5** — a schedule can only be expressed as a repeating daily beat if every
  cycle divides 24 hours, so that is the default cycle set.
"""
