"""`AutoScoutService.send_scouts_to_targets` had no test at all, and it holds
the entire scout budget cap: with `check_available=True` (the default), the
number of targets dispatched to must never exceed `scouts // scout_amount`.

`web/ws/scout_ws.py` re-implements this cap separately (see
`test_scout_ws_preflight.py`) -- this file covers the service method itself,
which the WS layer is not the only caller of.
"""

import asyncio
from types import SimpleNamespace

from travian_api.models.farm_list import MapTileInfo
from travian_api.services.auto_scout_service import AutoScoutService


def test_the_sweep_is_capped_by_the_scouts_that_exist(monkeypatch):
    scouted: list[tuple[int, int]] = []

    class _Military:
        def __init__(self, *a, **k):
            pass

        async def get_available_troops(self, village_id=None):
            return {"t4": 7}  # seven scouts

        async def send_scouts(self, *, x, y, amount, scout_type, village_id):
            scouted.append((x, y))
            return SimpleNamespace(success=True, raw_response="", travel_time="0:10:00")

    class _Resolver:
        def __init__(self, *a, **k):
            pass

    async def _none(*a, **k):
        return None

    # Both are imported INSIDE the method, so patch them at their own modules.
    monkeypatch.setattr("travian_api.services.military_service.MilitaryService", _Military)
    monkeypatch.setattr("travian_api.services.target_resolver.TargetResolver", _Resolver)
    monkeypatch.setattr("travian_api.services.auto_scout_service.asyncio.sleep", _none)

    class _Client:
        noise_injector = SimpleNamespace(maybe_inject_noise=_none)

        def tempo_scale(self, seconds):
            return seconds  # identity: HumanTiming.delay(0.0) divides by zero

    svc = AutoScoutService(http_client=_Client())
    targets = [
        MapTileInfo(x=i, y=0, village_id=100 + i, player_id=5, population=50, distance=float(i))
        for i in range(5)
    ]
    asyncio.run(svc.send_scouts_to_targets(targets, scout_amount=3, village_id=1))

    assert scouted == [(0, 0), (1, 0)], "7 scouts at 3 each is 2 targets, not 3 and not 5"
