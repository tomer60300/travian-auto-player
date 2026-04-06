# Real Test: Building Construct
Date: 2026-04-05

## Verdict: SKIP

## Reason
Queue is occupied by Clay Pit slot 16 upgrading to level 5 (from real-building-upgrade test).
Constructing a new building while queue is occupied would require --allow-gold, which violates safety rules.

## Safety Rule Applied
"Gold guard: Never pass --allow-gold. Queue occupied → SKIP the test."
