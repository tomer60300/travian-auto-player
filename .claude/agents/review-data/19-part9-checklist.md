# Part IX — Reviewer's checklist

## Constants and calibration
1. Is every capacity and production constant read live or configurable — no literals?
2. Is merchant capacity pinned by ≥2 observations at different Trade Office levels, or still only one?
3. Is merchant *speed* measured on this server, or assumed from stock?
4. Is Trade Office applied additively on base, and Commerce — if modelled at all — multiplicatively?
5. Does an unknown Trade Office default to 0 and round **down**, never up?
6. Are troop carry capacities verified against a full-bag report rather than assumed stock?

## Reads
7. Is net crop `l4`, never `l5` / free crop?
8. Can any net-crop path return `0` instead of `None`?
9. Is the filling branch of the countdown derivation still unvalidated — and does the code say so where it's used?
10. Is `merchants_total` the *second* number of the free/total cell?
11. Are in-transit resources reconciled exactly once, not double-counted?
12. Does every fetch run the §I.1.3 validations — all-zero rejection, missing villages, new villages, staleness, sum-to-total?

## Allocation
13. Is cargo the gap (`target − own_production`), clamped ≥ 0, everywhere including sustain mode?
14. Is the sign convention explicit at every comparison against zero?
15. Is exactly one `remainder` village per resource structurally enforced, not merely validated?

## Optimizer
16. Does the cycle sweep cover the full allowed set without assuming monotonicity in either direction?
17. Is `sets = ceil(rt / cycle)` correct at the boundary?
18. Are permanently-committed merchants subtracted **before** the reserve?
19. Do the flat reserve and the proportional headroom compose rather than double-count?
20. Does the infeasible path **refuse** rather than trim a route to fit?
21. Does the objective know that resources differ in scarcity, or does it treat wood and clay as interchangeable at equal merchant cost?

## Schedule
22. Is the beat a 1440-minute timeline placing every firing, not a minute-of-hour table?
23. Are repeat intervals restricted to {1,2,3,4,6,8,12,24} and validated at the API boundary?
24. Is the cost of that restriction reported rather than silently imposed?
25. Is `deliveries` distinguished from merchant count?

## Geometry
26. Is span 401 (not 801), odd-validated, and wrapped on both axes?
27. Does `map_id_to_coords` use the same span as the distance function?
28. Is merchant speed free of any Tournament Square, artifact, or hero term?

## Execute and revert
29. Is the full pre-write inventory recorded per origin before any write?
30. Does disable always precede delete, and are the two halves reported separately?
31. Does any report present write-history as delivery-history?
32. Does the shortfall assumption default to "skip," not "retry" or "top up"?

## Destination legality
33. Does anything classify a route target as own village / WW / alliance artifact / other player — and refuse the last?
34. Is pushing protection applied to cross-player sends and **not** to own-village transfers?

## Raiding
35. Is there an account-wide in-transit troop counter capped at 20,000, including returning legs?
36. Is the bag percentage treated as carry-utilisation, not as "fraction of target taken"?
37. Is siege excluded from farm-list entries?
38. Is the hero dip either gated on hero presence, or explicitly out of scope?
39. Are zero-bounty raids on CT2/CT3 targets treated as expected rather than as errors?
40. Is the "no-loss report suppression" option left **off**, given the tool parses reports?
41. Is send order taken from the rendered DOM order with sort explicitly pinned?
42. Is "abandoned farm list" a supported state in the data model?

## Buildings
43. Is the queue-slot model tribe-correct (Roman 2, others 1) plus the Plus waiting loop?
44. Is every gold-spending path behind the explicit opt-in — Master Builder, instant complete, NPC, production bonus?
45. Does the tool avoid running a Mead Festival during operations needing targeted catapults or chiefing?

## Expressive power — is the model as wide as the game?
46. Does the cadence decision space cover divisors of 1440, or only the eight native intervals?
47. When a route is storage-unsafe, can the tool split the same rate onto a finer cadence, or does it only escalate?
48. Does the tool model **rows** or **routes** — i.e. can two rows of one logical route carry different cargo?
49. Is arrival scheduling emitted as Deliver-at, or does the tool own travel-time arithmetic it could delegate?
50. Is the resource **mix** ever treated as a decision variable via NPC, or are the four resources fixed independent demands?
51. Is `deliveries` available as a lever if a per-village row cap turns out to bind?

## Player-decision quality — would a good player have done this?
52. Does any automation loop consult **time of day** before acting?
53. Does it consult **incoming attacks**, or act identically under threat?
54. Does it consult **recent report outcomes** to prune targets, or send until losses accumulate?
55. Before a long idle window, does it prefer long-distance sends so troops return at session start?
56. Does anything avoid leaving a warehouse at or near cap before an unattended period?
57. Does the night profile measure from an emptied pre-sleep baseline, or from an arbitrary snapshot?
58. Is the merchant reserve generous enough to answer an unpredictable alliance call?
59. Would the tool run the Brewery through a chiefing or targeted-catapult operation?
60. Is a surprising optimizer answer shown with the evidence that produced it, or asserted bare?
61. Are ambiguous operator-supplied figures flagged and excluded rather than assumed?

## Resilience
62. Does a conquered or removed village fail safe across the planner, farm lists, and build queue?
63. Is the golden fixture frozen at a snapshot, or does it read current state?
64. Does drift beyond threshold produce a re-plan diff, or only an alert?
65. Would a Diet Control artifact activating or lapsing invalidate any cached crop figure?
66. Does anything assume the tool is the only actor on the account, given duals and manual edits?

---

