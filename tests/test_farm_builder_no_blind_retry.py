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


def _run(farm_svc, military, reports):
    svc = _service(farm_svc, military, reports)
    return asyncio.run(svc.run_full(CONFIG, SURVIVORS, _noop_log, _never_stop))


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
