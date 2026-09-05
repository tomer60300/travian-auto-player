"""The farm builder must not re-send a write the transport refuses to re-send.

`farm_list_service.add_slot` and `military_service._send_troops` both pass
`safe_to_retry=False`, and `post_json` then raises

    "the session appears to have expired around a non-retryable request;
     refusing to re-send it because the original may already have taken effect"

for exactly the case where the write may have landed and the answer was lost.
The builder wrapped both in `for attempt in range(3)` and caught that refusal,
so one lost answer put three identical slots in the list (or three scouts on
one coordinate) and reported a single failure.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError
from travian_api.models.military import TroopSendResult
from travian_api.services.farm_builder_service import FarmBuilderService

LOST_ANSWER = NetworkError(
    "the session appears to have expired around a non-retryable request; refusing "
    "to re-send it because the original may already have taken effect"
)
REFUSED = NetworkError('HTTP 400: {"error":"badRequest"}', 400)

CONFIG = {"home_villages": [{"id": 1, "short": "V1"}], "per_home_lists": {"1": [{"name": "A"}]}}
SURVIVORS = [{"x": 10, "y": 20, "assigned_bucket": "A", "population": 100}]


@pytest.fixture(autouse=True)
def _no_real_pauses(monkeypatch):
    monkeypatch.setattr(
        "travian_api.stealth.timing.HumanTiming.micro_jitter",
        staticmethod(lambda *a, **k: 0),
    )
    monkeypatch.setattr(
        "travian_api.stealth.timing.HumanTiming.delay",
        staticmethod(lambda *a, **k: 0),
    )


class _FarmSvc:
    def __init__(self, add_raises=None):
        self._add_raises = add_raises
        self.adds: list[tuple[int, int, int]] = []
        self.created: list[str] = []

    async def get_all_farm_lists(self):
        return []

    async def create_farm_list(self, village_id, name, default_units=None):
        self.created.append(name)
        return 900 + len(self.created)

    async def add_slot(self, list_id, x, y, units=None, active=True, force=False):
        self.adds.append((list_id, x, y))
        if self._add_raises is not None:
            raise self._add_raises


class _Military:
    def __init__(self, result):
        self._result = result
        self.sends: list[tuple[int, int]] = []

    async def send_scouts(self, x, y, amount, scout_type="resources", village_id=None):
        self.sends.append((x, y))
        return self._result


class _Reports:
    """Answers 5a with a battle report on file, so ASSIGN is reached directly."""

    def __init__(self, combat_strength=20):
        self._cs = combat_strength

    async def fetch_village_reports(self, x, y, fetch_details=False):
        if self._cs is None:
            return {"reports": []}
        return {"reports": [{"icon_type": 3, "report_id": "r1", "aid": "a1"}]}

    async def fetch_report_detail(self, report_id):
        return {
            "type": "battle",
            "data": SimpleNamespace(
                defender_troops={"t1": 1},
                defender_combat_strength=self._cs,
            ),
        }


def _service(farm_svc, military, reports):
    session = SimpleNamespace(
        tribe_id=2,
        http_client=SimpleNamespace(),
        scout_service=None,
        farm_service=farm_svc,
        military_service=military,
        reports_service=reports,
    )
    return FarmBuilderService(session)


async def _noop_log(*args, **kwargs):
    return None


async def _never_stop():
    return False


def _run(farm_svc, military, reports, survivors=SURVIVORS, check_stop=_never_stop):
    svc = _service(farm_svc, military, reports)
    return asyncio.run(svc.run_full(CONFIG, survivors, _noop_log, check_stop))


# ── add_slot ──────────────────────────────────────────────────────────────


def test_a_clean_add_issues_one_post():
    farm_svc = _FarmSvc()
    report = _run(farm_svc, _Military(None), _Reports())
    assert len(farm_svc.adds) == 1
    assert report["added"] == 1
    assert report["failed"] == 0


def test_a_lost_add_answer_is_not_re_sent_and_is_reported_unverified():
    farm_svc = _FarmSvc(add_raises=LOST_ANSWER)
    report = _run(farm_svc, _Military(None), _Reports())

    # One attempt only: the slot may already be in the list.
    assert farm_svc.adds == [(901, 10, 20)]
    assert report["added"] == 0
    assert report["failed"] == 1
    assert "unverified" in report["failed_targets"][0]["reason"]


def test_a_refused_add_is_not_re_sent_either_and_is_reported_failed():
    farm_svc = _FarmSvc(add_raises=REFUSED)
    report = _run(farm_svc, _Military(None), _Reports())

    assert farm_svc.adds == [(901, 10, 20)]
    assert report["added"] == 0
    assert report["failed"] == 1
    assert "unverified" not in report["failed_targets"][0]["reason"]


# ── send_scouts ───────────────────────────────────────────────────────────


def test_a_lost_scout_answer_dispatches_once():
    military = _Military(
        TroopSendResult(
            success=False,
            target_x=10,
            target_y=20,
            raw_response="Connection reset (non-retryable): peer closed",
        )
    )
    farm_svc = _FarmSvc()
    _run(farm_svc, military, _Reports(combat_strength=None))
    assert military.sends == [(10, 20)]


def test_a_refused_scout_dispatches_once():
    military = _Military(
        TroopSendResult(
            success=False,
            target_x=10,
            target_y=20,
            raw_response='<div class="error">Not enough troops</div>',
        )
    )
    farm_svc = _FarmSvc()
    _run(farm_svc, military, _Reports(combat_strength=None))
    assert military.sends == [(10, 20)]


def test_a_pre_dispatch_failure_is_still_retried():
    """Step 1 returned no confirmation form, so nothing was dispatched.

    That is the one shape `auto_scout_service` retries, and it stays safe here.
    """
    military = _Military(
        TroopSendResult(
            success=False,
            target_x=10,
            target_y=20,
            raw_response="Step 1 error: No confirmation form returned (rate limit)",
        )
    )
    farm_svc = _FarmSvc()
    _run(farm_svc, military, _Reports(combat_strength=None))
    assert military.sends == [(10, 20), (10, 20), (10, 20)]


# ── F9: a stop during the assign phase is a STOPPED run ──────────────────


def test_a_stop_during_the_assign_phase_is_reported_as_stopped():
    """`ws/farm_builder` reads `report.get("stopped")` to pick the final state.

    The assign phase's `if await check_stop(): break` broke only the INNER
    record loop (so it skipped to the next bucket rather than ending the run)
    and the report carried no `stopped` key at all. A stop after 2 of 6 adds
    was pushed to the UI and persisted in `FarmBuilderRunHistory.status` as
    `completed` with `added=2`, and there is no total-vs-added reconciliation
    on the page. Every other early return in the service returns
    `{"stopped": True}`.
    """
    survivors = [
        {"x": 10 + i, "y": 20, "assigned_bucket": "A", "population": 100} for i in range(6)
    ]
    farm_svc = _FarmSvc()

    async def _stop_after_two_adds():
        return len(farm_svc.adds) >= 2

    report = _run(
        farm_svc,
        _Military(None),
        _Reports(),
        survivors=survivors,
        check_stop=_stop_after_two_adds,
    )

    assert len(farm_svc.adds) == 2, "the stop arrived after 2 of 6"
    assert report.get("stopped") is True
    assert report["added"] == 2
    assert report["total_targets"] == 6


# ── F11: a slot-limit refusal on the last attempt ────────────────────────


def _slot_limit_error():
    from travian_api.services.farm_list_service import FarmListMutationRefused

    return FarmListMutationRefused(
        "the add of slot (10,20) to list 901 was refused by the game: errorRaidListSlotLimit"
    )


def test_a_slot_limit_refusal_on_every_attempt_is_accounted_for():
    """The `errorRaidListSlotLimit` branch `continue`d without consuming a
    `fail_add` entry, so on the last attempt the loop ended with `ok = False`
    and nothing appended: the report's own arithmetic did not add up
    (`added+skipped+failed=0` for `total_targets=1`) and nothing in it or in
    the log named the target. `:1004-1005` was a no-op that was meant to close
    this (`if not ok and not fail_add or (...): pass  # already handled`).
    """
    farm_svc = _FarmSvc(add_raises=_slot_limit_error())
    report = _run(farm_svc, _Military(None), _Reports())

    assert report["total_targets"] == 1
    accounted = report["added"] + report["skipped"] + report["failed"]
    assert accounted == 1, f"accounted for {accounted}/1"
    assert "slot_limit" in report["failed_targets"][0]["reason"]
    assert (report["failed_targets"][0]["x"], report["failed_targets"][0]["y"]) == (10, 20)


def test_a_target_that_can_never_be_added_probes_one_overflow_list_only():
    """Every refusal created another overflow list to put nothing in.

    Measured: 3 add POSTs, 4 lists created (`A`, `A-2`, `A-3`, `A-4`) — three
    of them empty, persisting in the game with no undo endpoint. On a full
    account this multiplies by the number of affected targets.

    A list created seconds ago that refuses a slot means the ACCOUNT is out of
    capacity, so exactly one such probe is made and the finding is latched for
    the rest of the run.
    """
    farm_svc = _FarmSvc(add_raises=_slot_limit_error())
    _run(farm_svc, _Military(None), _Reports())

    assert farm_svc.created == ["A", "A-2"], f"orphan lists: {farm_svc.created[2:]}"
    assert len(farm_svc.adds) == 2, "one add per list, no third"


def test_the_slot_limit_latch_stops_more_lists_for_later_targets():
    survivors = [
        {"x": 10 + i, "y": 20, "assigned_bucket": "A", "population": 100} for i in range(4)
    ]
    farm_svc = _FarmSvc(add_raises=_slot_limit_error())
    report = _run(farm_svc, _Military(None), _Reports(), survivors=survivors)

    assert farm_svc.created == ["A", "A-2"], f"one probe for the run: {farm_svc.created}"
    assert report["failed"] == 4, "every target is still reported"
