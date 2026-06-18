"""Regression tests for build-queue idle-wait selection.

Guards the h1 double-delay fix: the resource-short "come back later" pause must
be a SINGLE heavy-tailed value clamped to [120, 600]s, never the compounded
delay-of-a-delay that once produced 45+ minute frozen waits.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_resource_short_wait_is_single_clamped_not_compounded():
    from travian_api.services.build_queue_service import _resolve_idle_wait

    random.seed(0)
    for _ in range(5000):
        reason, wait_s = _resolve_idle_wait(
            any_no_checksum=False,
            any_resource_short=True,
            next_prio=1,
            poll_interval_s=30.0,
            stealth_enabled=True,
        )
        assert "Insufficient resources" in reason
        # Single heavy-tail clamped to [2, 10] min — NOT compounded into thousands.
        assert 120.0 <= wait_s <= 600.0


def test_resource_short_wait_stealth_off_is_poll_interval():
    from travian_api.services.build_queue_service import _resolve_idle_wait

    reason, wait_s = _resolve_idle_wait(
        any_no_checksum=False,
        any_resource_short=True,
        next_prio=2,
        poll_interval_s=30.0,
        stealth_enabled=False,
    )
    assert wait_s == 30.0


def test_transient_branches_are_single_heavy_tail_around_poll():
    from travian_api.services.build_queue_service import _resolve_idle_wait

    # HumanTiming.delay(mean) clamps to [mean*0.1, mean*15] = [3, 450] for mean=30.
    random.seed(1)
    for _ in range(3000):
        reason, w = _resolve_idle_wait(
            any_no_checksum=True,
            any_resource_short=False,
            next_prio=1,
            poll_interval_s=30.0,
            stealth_enabled=True,
        )
        assert "no upgrade URL" in reason
        assert 3.0 <= w <= 450.0
    for _ in range(3000):
        reason, w = _resolve_idle_wait(
            any_no_checksum=False,
            any_resource_short=False,
            next_prio=1,
            poll_interval_s=30.0,
            stealth_enabled=True,
        )
        assert "No items completed" in reason
        assert 3.0 <= w <= 450.0


def test_resource_short_clamp_boundaries(monkeypatch):
    """Deterministic boundary guard: resource-short wait clamps to [120, 600]s
    regardless of the underlying heavy-tailed draw (and is never re-delayed)."""
    from travian_api.services import build_queue_service as bq

    monkeypatch.setattr(bq.HumanTiming, "delay", staticmethod(lambda *a, **k: 5.0))
    _, low = bq._resolve_idle_wait(
        any_no_checksum=False, any_resource_short=True, next_prio=1,
        poll_interval_s=30.0, stealth_enabled=True,
    )
    assert low == 120.0  # tiny draw clamps up

    monkeypatch.setattr(bq.HumanTiming, "delay", staticmethod(lambda *a, **k: 5000.0))
    _, high = bq._resolve_idle_wait(
        any_no_checksum=False, any_resource_short=True, next_prio=1,
        poll_interval_s=30.0, stealth_enabled=True,
    )
    assert high == 600.0  # huge draw clamps down
