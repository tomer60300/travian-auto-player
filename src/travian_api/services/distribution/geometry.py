"""Toroidal map geometry: distances and travel times between villages.

Pure functions. The Travian map wraps at its edges, so the shortest path between
two villages may cross the boundary rather than run through the middle -- a
village at x=-190 and one at x=190 are neighbours on a 401-wide map, not 380
fields apart.

WORLD COMPATIBILITY -- constraints, not defects, recorded from the 2026-08-27
external review so nobody rediscovers them the hard way:

* Some special Travian worlds have HARD map edges. Everything here always
  wraps, so distances near the seam are wrong on those worlds -- routes get
  planned as short that are actually long. There is no flag for it; running
  the planner on a hard-edge world is unsupported.
* Special worlds may add trade SHIPS, which move alongside merchants on water
  routes. The live create payload pins useTradeShips: false and no capacity
  model for them exists, so on a ship world the planner under-uses what the
  account could actually move.
* Merchant capacity, speed, and the Trade Office / alliance-Commerce bonuses
  vary by tribe and world. The merchant-model calibration fields on the
  request are what absorb that; a wrong calibration changes feasibility
  directly, which is why the UI surfaces those numbers instead of hiding them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Coord = tuple[int, int]


@dataclass(frozen=True)
class MapGeometry:
    """Distance and travel-time model for one server.

    Args:
        span: full map width in fields. Travian coordinates run -N..+N, so a
            map whose corners are +/-200 has ``span = 401``.
        speed_fields_per_hour: merchant speed. **Tribe-specific** (Teuton 12,
            Roman 16, Gaul 24 at 1x) -- it belongs with the tribe, not in
            global config, see review R9.
    """

    span: int
    speed_fields_per_hour: float

    def __post_init__(self) -> None:
        if self.span <= 0:
            raise ValueError(f"span must be positive, got {self.span}")
        if self.speed_fields_per_hour <= 0:
            raise ValueError(f"speed must be positive, got {self.speed_fields_per_hour}")

    def _axis_delta(self, a: int, b: int) -> int:
        """Shortest separation on one wrapped axis.

        A raw separation wider than the map is not a distance, it is a
        coordinate that does not exist on this world: `span - raw` goes
        negative, `min` returns it, and `math.hypot` takes the absolute value --
        so (450|0) against (0|0) on a 401-wide map read as 49 fields, a
        five-minute haul to nowhere. Refused rather than folded, because a
        foreign target's coordinates decide which villages can reach it and
        therefore what the plan does.
        """
        raw = abs(a - b)
        if raw > self.span:
            raise ValueError(
                f"{a} and {b} are {raw} fields apart, which is off a {self.span}-wide map: "
                f"coordinates run -{(self.span - 1) // 2}..+{(self.span - 1) // 2}"
            )
        return min(raw, self.span - raw)

    def distance(self, origin: Coord, target: Coord) -> float:
        """Euclidean distance in fields, taking the wrap into account."""
        return math.hypot(
            self._axis_delta(origin[0], target[0]),
            self._axis_delta(origin[1], target[1]),
        )

    def one_way_minutes(self, origin: Coord, target: Coord) -> float:
        """Travel time one way, in minutes."""
        return self.distance(origin, target) / self.speed_fields_per_hour * 60.0

    def round_trip_minutes(self, origin: Coord, target: Coord) -> float:
        """Travel time there and back, in minutes.

        This is what sizes a merchant pool: a merchant is unavailable for the
        whole round trip, not just the delivery.
        """
        return 2.0 * self.one_way_minutes(origin, target)
