"""Toroidal map geometry.

The wrap matters: on a 401-wide map the two far edges are adjacent, so a naive
Euclidean distance overstates the trip for villages near opposite borders --
and an overstated trip inflates the merchant pool for exactly the routes that
are already expensive.
"""

import math

import pytest

from travian_api.services.distribution.geometry import MapGeometry

# Europe-style map: coordinates -200..+200.
EUROPE = MapGeometry(span=401, speed_fields_per_hour=12.0)


class TestDistance:
    def test_same_village_is_zero(self):
        assert EUROPE.distance((15, 91), (15, 91)) == 0.0

    def test_plain_euclidean_when_the_wrap_is_not_shorter(self):
        assert EUROPE.distance((0, 0), (3, 4)) == pytest.approx(5.0)

    def test_shortest_path_crosses_the_map_edge(self):
        """x=-198 and x=198 are 5 fields apart on a 401-wide map, not 396."""
        assert EUROPE.distance((-198, 0), (198, 0)) == pytest.approx(5.0)

    def test_wrap_applies_on_both_axes_at_once(self):
        assert EUROPE.distance((-199, -199), (199, 199)) == pytest.approx(math.hypot(3, 3))

    def test_distance_is_symmetric(self):
        a, b = (42, 17), (-64, 116)

        assert EUROPE.distance(a, b) == pytest.approx(EUROPE.distance(b, a))

    def test_no_pair_exceeds_half_the_map_on_an_axis(self):
        """A wrapped separation can never exceed half the map."""
        for x in range(-200, 201, 37):
            assert EUROPE.distance((0, 0), (x, 0)) <= EUROPE.span / 2


class TestTravelTime:
    def test_one_way_minutes_from_speed(self):
        """12 fields at 12 fields/hour is one hour."""
        assert EUROPE.one_way_minutes((0, 0), (12, 0)) == pytest.approx(60.0)

    def test_round_trip_is_twice_one_way(self):
        origin, target = (0, 0), (30, 40)

        assert EUROPE.round_trip_minutes(origin, target) == pytest.approx(
            2 * EUROPE.one_way_minutes(origin, target)
        )

    def test_faster_tribe_travels_the_same_distance_sooner(self):
        gaul = MapGeometry(span=401, speed_fields_per_hour=24.0)

        assert gaul.one_way_minutes((0, 0), (12, 0)) == pytest.approx(30.0)


class TestValidation:
    @pytest.mark.parametrize("span,speed", [(0, 12.0), (-1, 12.0), (401, 0), (401, -3.0)])
    def test_invalid_geometry_is_rejected(self, span, speed):
        with pytest.raises(ValueError):
            MapGeometry(span=span, speed_fields_per_hour=speed)

    def test_a_coordinate_off_the_map_is_refused_not_folded(self):
        """`min(raw, span - raw)` goes NEGATIVE once raw exceeds the span, and
        `hypot` takes the absolute value of it -- so (450|0) against (0|0) on a
        401-wide map read as 49 fields, a five-minute haul to a place that is
        not on the map at all. Now load-bearing: a foreign target's coordinates
        decide which villages can reach it and therefore what gets planned."""
        with pytest.raises(ValueError, match="off a 401-wide map"):
            EUROPE.distance((0, 0), (450, 0))

    def test_the_widest_legal_separation_is_still_measured(self):
        """The boundary, so the refusal cannot creep inwards: -200 to 200 is
        400 apart raw and one field by the wrap."""
        assert EUROPE.distance((-200, 0), (200, 0)) == pytest.approx(1.0)
