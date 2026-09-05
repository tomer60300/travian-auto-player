"""A dry run must not report raids as sent.

`stats["sent"] += 1` sat inside the `if config.dry_run:` branch, and the
summary the WS pushes (`ws/oasis_raider.py`) carries no `dry_run` key at all —
so `OasisRaider.jsx`'s "Raids sent: {summary.sent}" card read identically for a
dry run and a live run of the same config. The per-target log lines do say
"DRY RUN ... Would raid", but the summary is the artifact that survives the
scroll, and `handleStart(true)` / `handleStart(false)` are two adjacent
buttons.

Measured before the fix, 5 empty oases, same config bar the flag:
    DRY RUN : game raid POSTs = 0   summary.sent = 5
    LIVE RUN: game raid POSTs = 5   summary.sent = 5
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.oasis_raider_service import OasisRaiderConfig, OasisRaiderService

TROOPS = {"t1": 5}


class _Oasis:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.distance = float(abs(x) + abs(y))


class _Military:
    async def get_available_troops(self, village_id=None):
        return {"t1": 1000}


def _service(monkeypatch, oases):
    session = SimpleNamespace(
        tribe_id=2,
        active_village_id=1,
        http_client=SimpleNamespace(base_url="https://example.invalid"),
        military_service=_Military(),
        scout_service=None,
        auth_state=SimpleNamespace(villages=[SimpleNamespace(id=1, x=0, y=0)]),
    )
    svc = OasisRaiderService(session)
    raids: list[tuple[int, int]] = []

    async def _scan(radius, cx, cy):
        return list(oases)

    async def _detail(x, y):
        return {
            "bonus": "25% Crop",
            "troops": {},
            "troops_str": "none",
            "distance": 1.0,
            "has_any_troops": False,
        }

    async def _send(x, y, troops, village_id=None):
        raids.append((x, y))
        return SimpleNamespace(success=True, raw_response="")

    monkeypatch.setattr(svc, "_scan_for_oases", _scan)
    monkeypatch.setattr(svc, "_fetch_oasis_detail", _detail)
    monkeypatch.setattr(svc, "_send_raid", _send)
    # The sweep's stochastic stealth steps are not what is under test.
    monkeypatch.setattr(svc, "_should_randomly_skip", lambda remaining: False)

    async def _no_think(send_log, check_stop):
        return 0.0

    async def _no_break(send_log, check_stop, village_id=None):
        return 0.0

    async def _no_browse(send_log, village_id=None):
        return None

    monkeypatch.setattr(svc, "_human_think_delay", _no_think)
    monkeypatch.setattr(svc, "_take_micro_break", _no_break)
    monkeypatch.setattr(svc, "_simulate_map_browsing", _no_browse)
    monkeypatch.setattr("travian_api.services.oasis_raider_service._sample_burst_size", lambda: 999)
    monkeypatch.setattr(
        "travian_api.services.oasis_raider_service.BROWSE_FREQ_MIN", 999, raising=False
    )
    monkeypatch.setattr(
        "travian_api.services.oasis_raider_service.BROWSE_FREQ_MAX", 1000, raising=False
    )
    return svc, raids


async def _noop_log(*args, **kwargs):
    return None


async def _never_stop():
    return False


def _run(monkeypatch, *, dry_run, count=5, max_targets=0):
    oases = [_Oasis(10 + i, 20) for i in range(count)]
    svc, raids = _service(monkeypatch, oases)
    config = OasisRaiderConfig(
        radius=5, troops=dict(TROOPS), dry_run=dry_run, max_targets=max_targets
    )
    stats = asyncio.run(svc.run_sweep(config, _noop_log, _never_stop))
    return stats, raids


def test_a_live_run_reports_what_it_sent(monkeypatch):
    stats, raids = _run(monkeypatch, dry_run=False)
    assert len(raids) == 5
    assert stats["sent"] == 5
    assert stats["would_send"] == 0
    assert stats["dry_run"] is False


def test_a_dry_run_reports_nothing_as_sent(monkeypatch):
    stats, raids = _run(monkeypatch, dry_run=True)
    assert raids == [], "a dry run issues no raid POSTs"
    assert stats["sent"] == 0, "nothing was sent"
    assert stats["would_send"] == 5
    assert stats["dry_run"] is True


def test_the_summary_says_which_kind_of_run_it_was(monkeypatch):
    dry, _ = _run(monkeypatch, dry_run=True)
    live, _ = _run(monkeypatch, dry_run=False)
    assert dry != live, "a dry run and a live run must not produce equal summaries"
    assert "dry_run" in dry


@pytest.mark.parametrize("dry_run", [True, False])
def test_max_targets_still_bounds_both_kinds_of_run(monkeypatch, dry_run):
    """`max_targets` gated on `sent`, which a dry run no longer increments."""
    stats, raids = _run(monkeypatch, dry_run=dry_run, count=5, max_targets=2)
    assert stats["sent"] + stats["would_send"] == 2
    assert len(raids) == (0 if dry_run else 2)
