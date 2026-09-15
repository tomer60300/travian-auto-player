"""Pacing bounds the RATE. It does nothing about DURATION, and that is separate.

Measured off the 2026-09-15 recording, over 364 deliberate requests (documents
and API calls; the 1,265 locale bundles are cache hits and never touch the
network)::

    sustained                 0.325 req/s   -- one decision every 3.1s
    densest 60-second window  0.88  req/s

Our 25-region scan runs at 0.67 req/s, which sits below that densest minute and
twice the sustained figure. That is fine for forty seconds -- humans burst. It
is not fine for twelve minutes, and `enrich_tiles` is a several-hundred-tile
loop that awaited nothing but the next request: no break, no budget check, no
stop check, while the oasis sweep beside it has had burst-and-break for months.

A human sustains 0.33 req/s across a session and bursts above it briefly. They
do not make three hundred consecutive decisions without looking at anything
else. Twelve unbroken minutes of map clicks is not a fast human; it is a shape
no human has.

Which is why the answer to "our scan is too fast" was never "slow the requests
down further" -- past a point that is just a machine being patient. It is to
make a long operation look like several visits.
"""

import asyncio
import statistics
from types import SimpleNamespace

from travian_api.services.auto_scout_service import _tiles_before_a_pause


class TestThePauseIntervalIsNotAPeriod:
    def test_it_is_never_a_fixed_count(self):
        draws = {_tiles_before_a_pause() for _ in range(500)}

        assert len(draws) > 10, "a break every exactly-N tiles is a period, and a "
        "period is the easiest thing in the world to find in a timestamp series"

    def test_it_is_right_skewed_rather_than_flat(self):
        draws = [_tiles_before_a_pause() for _ in range(4000)]

        assert statistics.mean(draws) > statistics.median(draws)

    def test_it_never_degenerates_to_pausing_constantly(self):
        assert min(_tiles_before_a_pause() for _ in range(2000)) >= 6

    def test_its_body_sits_where_a_person_would_stop(self):
        median = statistics.median(_tiles_before_a_pause() for _ in range(4000))

        assert 12 <= median <= 26


class TestALongRunActuallyBreaks:
    def _service(self, tiles: int):
        from travian_api.services.auto_scout_service import AutoScoutService

        svc = AutoScoutService.__new__(AutoScoutService)
        svc._report = lambda *_a, **_k: None
        return svc

    def test_a_long_sweep_takes_several_breaks(self, monkeypatch):
        import travian_api.services.auto_scout_service as mod

        svc = self._service(300)
        slept: list[float] = []

        async def _sleep(s):
            slept.append(s)

        monkeypatch.setattr(mod.asyncio, "sleep", _sleep)

        async def _details(x, y):
            return SimpleNamespace(
                distance=0,
                is_oasis=False,
                is_abandoned=False,
                village_name="",
                player_id=None,
                player_name="",
                alliance_id=None,
                alliance_name="",
            )

        svc.get_tile_details = _details
        tiles = [
            SimpleNamespace(
                x=i,
                y=0,
                distance=1,
                is_oasis=False,
                is_abandoned=False,
                village_name="v",
                player_id=1,
                player_name="p",
                alliance_id=2,
                alliance_name="a",
            )
            for i in range(300)
        ]

        asyncio.run(svc.enrich_tiles(tiles))

        assert len(slept) >= 8, (
            "300 tiles in one unbroken run is twelve minutes of continuous "
            "clicking, which is the shape this exists to break up"
        )
        assert all(12.0 <= s <= 240.0 for s in slept)

    def test_a_short_sweep_is_not_interrupted(self, monkeypatch):
        """Five tiles is a person looking at five tiles. Breaking that up would
        invent a hesitation nobody has."""
        import travian_api.services.auto_scout_service as mod

        svc = self._service(5)
        slept: list[float] = []

        async def _sleep(s):
            slept.append(s)

        monkeypatch.setattr(mod.asyncio, "sleep", _sleep)

        async def _details(x, y):
            return SimpleNamespace(
                distance=0,
                is_oasis=False,
                is_abandoned=False,
                village_name="",
                player_id=None,
                player_name="",
                alliance_id=None,
                alliance_name="",
            )

        svc.get_tile_details = _details
        tiles = [
            SimpleNamespace(
                x=i,
                y=0,
                distance=1,
                is_oasis=False,
                is_abandoned=False,
                village_name="v",
                player_id=1,
                player_name="p",
                alliance_id=2,
                alliance_name="a",
            )
            for i in range(5)
        ]

        asyncio.run(svc.enrich_tiles(tiles))

        assert slept == []
