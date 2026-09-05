"""`travian_distance` is the only distance function the raid analyzer has,
and every existing test sets `state.distance` by hand -- so the map's
wrap-around at the 401-wide seam has never actually been executed.
"""

import math

import pytest

from travian_api.services.raid_analyzer_service import travian_distance


def test_the_map_wraps_at_the_seam():
    # A 401-wide map runs -200..200, so -199 and 199 are THREE tiles apart
    # across the seam, not 398 the long way round.
    assert travian_distance(-199, 0, 199, 0) == pytest.approx(3.0)
    assert travian_distance(0, -199, 0, 199) == pytest.approx(3.0)
    assert travian_distance(-199, -199, 199, 199) == pytest.approx(math.hypot(3, 3))
    # ...and no wrap where none is due.
    assert travian_distance(0, 0, 3, 4) == pytest.approx(5.0)
