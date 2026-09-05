"""One slot must not empty a village of a unit, on the path the planner uses.

``SUPPLY_CLAIM_CAP = 0.90`` is documented as a safety floor -- "a single slot
cannot claim more than this fraction of the village's available supply of that
unit. Prevents one slot from draining V3 of all its TKs." Its only enforcement
was in ``compute_placement_score``, which is reachable only from
``pick_best_placement``, and that function's own docstring says plan_rebalance
stopped calling it at v5.0. The live path -- ``plan_rebalance`` ->
``plan_waves_for_target`` -- gated on ``count > avail`` alone, i.e. 100%.

So the comment described a live safety that no live code performed. Reading the
comment as the specification (the constant names a real hazard: a wave sized
from a rich target's avg_loot can want every Teutonic Knight V3 has, leaving the
village with nothing for its own defence or its next target), the cap belongs on
the wave path, and that is what these pin.

The check is against ``avail`` -- what is LEFT at that moment -- not the opening
supply, because ``plan_waves_for_target`` decrements as it commits: the second
target through a village must not be able to take 100% of the remainder either.
"""

from dataclasses import dataclass, field

import pytest

from travian_api.services.rebalance_planner import (
    SUPPLY_CLAIM_CAP,
    UNIT_CARRY,
    VillagePosition,
    plan_waves_for_target,
)


@dataclass
class FakeTarget:
    coord: tuple[int, int]
    avg_loot: float
    total_raids_all_lists: int = 0
    last_raid_time_unix: int | None = None
    target_name: str = ""
    primary_owner_village: str = "V1"
    slot_instances: list[tuple[str, int]] = field(default_factory=list)


# One village, one unit: the wave plan has exactly one candidate, so the only
# thing that can decide the outcome is the supply gate.
VPS = [VillagePosition("V7", 30, 82)]
COORD = (31, 83)
CLUB_CARRY = UNIT_CARRY["t1"]


def _loot_wanting(count: int) -> float:
    """The avg_loot whose first wave is sized at exactly *count* clubs.

    Wave 0 sizes at ceil(avg_loot * CARRY_SAFETY / carry), so land in the
    middle of the band rather than on its edge.
    """
    return (count - 0.5) * CLUB_CARRY / 1.10


def _plan(count_wanted: int, supply: int):
    supplies = {("V7", "t1"): supply}
    target = FakeTarget(coord=COORD, avg_loot=_loot_wanting(count_wanted))
    return plan_waves_for_target(target, VPS, supplies), supplies


def test_a_wave_may_take_ninety_of_a_hundred():
    waves, supplies = _plan(90, 100)
    assert [w.optimal_count for w in waves] == [90]
    assert supplies[("V7", "t1")] == 10


def test_a_wave_may_not_take_ninety_one_of_a_hundred():
    # The whole point: before the cap reached this path, 91 of 100 was fine and
    # so was 100 of 100 -- one slot emptying the village.
    waves, supplies = _plan(91, 100)
    assert waves == []
    assert supplies[("V7", "t1")] == 100, "a refused wave must not spend supply"


def test_a_wave_may_not_take_the_whole_village():
    waves, _ = _plan(100, 100)
    assert waves == []


@pytest.mark.parametrize("supply", [10, 250, 2506])
def test_the_cap_is_a_fraction_not_a_number(supply):
    """Whatever the pool, a slot leaves at least a tenth of it behind."""
    waves, _ = _plan(supply, supply)
    assert waves == []
    waves, _ = _plan(int(supply * SUPPLY_CLAIM_CAP), supply)
    assert [w.optimal_count for w in waves] == [int(supply * SUPPLY_CLAIM_CAP)]


def test_the_cap_binds_on_what_is_left_not_on_the_opening_supply():
    """Two targets through one village: the second sees the decremented pool."""
    supplies = {("V7", "t1"): 100}
    first = FakeTarget(coord=COORD, avg_loot=_loot_wanting(50))
    assert [w.optimal_count for w in plan_waves_for_target(first, VPS, supplies)] == [50]
    assert supplies[("V7", "t1")] == 50

    # 46 of the remaining 50 is over the cap (45), even though it is well under
    # 90% of the 100 the village started with.
    second = FakeTarget(coord=COORD, avg_loot=_loot_wanting(46))
    assert plan_waves_for_target(second, VPS, supplies) == []
    assert supplies[("V7", "t1")] == 50
