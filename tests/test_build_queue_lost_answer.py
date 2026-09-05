"""A build action whose answer was lost must not be issued a second time.

`building_service._upgrade_building_unlocked` performs the write as
`get_html(upgrade_url, safe_to_retry=False)` and turns any exception into
`UpgradeResult(success=False)`. `execute_plan_continuous` then reported the
failure, left the item `pending` at its OLD level and re-entered the loop —
which issued the upgrade again. Every guard the docstring advertises keys off
`is_queue_empty()` or the success branch, so none of them covers "the write
landed and I do not know it": one dropped response cost an extra level's
resources and an extra build slot, invisibly.

Measured before the fix: 2 upgrade writes, final level 6 against a plan target
of 5, and `results` reporting one clean `4->5`.
"""

import asyncio

import pytest

from travian_api.models.buildings import (
    Building,
    BuildingDetail,
    QueueItem,
    Resources,
    UpgradeResult,
)
from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem, BuildQueueService

SLOT = 19
NAME = "Warehouse"


@pytest.fixture(autouse=True)
def _no_real_pauses(monkeypatch):
    for name in ("delay", "micro_jitter", "reaction_time"):
        monkeypatch.setattr(
            f"travian_api.stealth.timing.HumanTiming.{name}",
            staticmethod(lambda *a, **k: 0),
        )


class _Game:
    """A village that answers each successive upgrade with a chosen verdict.

    Verdicts:
      "accepted" -- reports success, and the level moves.
      "lost"     -- reports failure, but the write LANDED anyway.
      "refused"  -- reports failure and nothing happened.

    `queue_reads` is how many construction-queue reads show a landed build
    still running before the level moves — 0 models a level that has already
    advanced when we look, 1+ models one that is still in the queue.
    """

    def __init__(self, verdicts, *, queue_reads=0, level=4):
        self.level = level
        self.upgrades: list[int] = []
        self._verdicts = list(verdicts)
        self._queue_reads = queue_reads
        self._pending_target = None
        self._reads_left = 0

    async def get_village_buildings(self, village_id=None):
        return [Building(slot_id=SLOT, gid=10, name=NAME, level=self.level)]

    async def get_construction_queue(self, village_id=None):
        if self._pending_target is None:
            return []
        if self._reads_left <= 0:
            self.level = self._pending_target
            self._pending_target = None
            return []
        self._reads_left -= 1
        return [
            QueueItem(
                event_id="e1",
                building_name=NAME,
                target_level=self._pending_target,
                remaining_seconds=1,
            )
        ]

    async def get_building_detail(self, slot_id, village_id=None):
        return BuildingDetail(
            slot_id=slot_id,
            gid=10,
            name=NAME,
            level=self.level,
            costs={"lumber": 10},
            construction_time="0:01:00",
            checksum="abc123",
            upgrade_url="/dorf2.php?a=19&c=abc123",
        )

    async def get_resources(self, village_id=None):
        return Resources(lumber=99999, clay=99999, iron=99999, crop=99999)

    async def upgrade_building(self, slot_id, allow_gold=False, village_id=None):
        self.upgrades.append(slot_id)
        old = self.level
        verdict = self._verdicts.pop(0) if self._verdicts else "accepted"
        assert verdict in ("accepted", "lost", "refused")
        if verdict != "refused":
            if self._queue_reads:
                self._pending_target = old + 1
                self._reads_left = self._queue_reads
            else:
                self.level = old + 1
        success = verdict == "accepted"
        raw = {
            "accepted": "",
            "lost": "Connection reset (non-retryable): peer closed",
            "refused": "UPGRADE FAILED: notEnough resources",
        }[verdict]
        return UpgradeResult(
            success=success,
            village_id=0,
            building_id=slot_id,
            building_name=NAME,
            old_level=old,
            new_level=old + 1 if success else old,
            construction_time="0:01:00",
            reward_used=False,
            raw_response=raw,
        )


class _Http:
    stealth_enabled = False

    def __init__(self):
        self.human_delay = _Delay()
        self.activity_scheduler = _Scheduler()
        self.noise_injector = _Noise()

    def tempo_scale(self, seconds):
        return 0


class _Delay:
    async def wait(self, action_type, reason=""):
        return None


class _Scheduler:
    def __init__(self):
        self.bills: list[float] = []

    def can_continue(self):
        return True

    def log_activity(self, seconds):
        self.bills.append(seconds)

    def next_break_duration(self):
        return 0

    def start_session(self):
        return None


class _Noise:
    async def maybe_inject_noise(self, village_id=None):
        return None


def _plan():
    return BuildPlan(
        village_id=0,
        items=[BuildPlanItem(building=NAME, target=5, priority=1, slot=SLOT)],
    )


def _run(game):
    svc = BuildQueueService(_Http())
    svc.building_service = game
    results = asyncio.run(svc.execute_plan_continuous(_plan(), poll_interval_s=1))
    return results


def test_an_accepted_upgrade_writes_once():
    game = _Game(["accepted"])
    results = _run(game)
    assert game.upgrades == [SLOT]
    assert game.level == 5
    assert [r["status"] for r in results] == ["started"]


def test_a_lost_answer_does_not_overshoot_the_target():
    """The level had already advanced when we looked."""
    game = _Game(["lost"])
    results = _run(game)

    assert game.upgrades == [SLOT], "the upgrade must not be re-issued"
    assert game.level == 5, "target was 5"
    assert [r["status"] for r in results] == ["started"]


def test_a_lost_answer_with_the_build_still_queued_does_not_overshoot():
    """The landed build is in the construction queue; the level has not moved."""
    game = _Game(["lost"], queue_reads=1)
    results = _run(game)

    assert game.upgrades == [SLOT], "the upgrade must not be re-issued"
    assert [r["status"] for r in results] == ["started"]
    assert [r["level"] for r in results] == ["4->5"]
    asyncio.run(game.get_construction_queue())  # let the queued build finish
    assert game.level == 5, "target was 5"


def test_an_unverifiable_failure_is_not_re_issued_either():
    """Neither re-read could be made, so whether the write landed is unknown."""

    class _Blind(_Game):
        """Both post-write reads fail once — exactly when the fix consults them."""

        def __init__(self, verdicts):
            super().__init__(verdicts)
            self._blind_queue = True
            self._blind_buildings = True

        async def get_construction_queue(self, village_id=None):
            if self.upgrades and self._blind_queue:
                self._blind_queue = False
                raise RuntimeError("queue read failed")
            return await _Game.get_construction_queue(self, village_id)

        async def get_village_buildings(self, village_id=None):
            if self.upgrades and self._blind_buildings:
                self._blind_buildings = False
                raise RuntimeError("building read failed")
            return await _Game.get_village_buildings(self, village_id)

    game = _Blind(["lost"])
    results = _run(game)

    assert game.upgrades == [SLOT], "an upgrade that MAY have landed is never re-issued"
    assert [r["status"] for r in results] == ["unverified"]


# ── F12: the machine-readable result must carry the failures too ──────────


def test_a_refused_upgrade_appears_in_the_results():
    """`execute_plan` appends a `failed` entry; the continuous twin did not.

    The refusal existed only as a transient `_report` status string, so
    `queue_ws`'s `ok = r.get("status") == "started"` branch was dead code and a
    cycle that refused an upgrade and then accepted it was indistinguishable
    from one that accepted it first time.
    """
    game = _Game(["refused", "accepted"])
    results = _run(game)

    assert game.upgrades == [SLOT, SLOT], "a genuine refusal IS safe to retry"
    assert game.level == 5
    assert [r["status"] for r in results] == ["failed", "started"]
    failed = results[0]
    assert failed["building"] == NAME
    assert failed["slot_id"] == SLOT
    assert "notEnough" in failed["error"]
