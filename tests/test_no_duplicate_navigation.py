"""A session does not re-load the page it is already looking at.

Two shapes of the same tell:

* The farm cycle groups its lists by owner village so the rally-point context
  holds across them -- then re-walked dorf2 -> rally point -> farm-list tab
  before every single list, issuing the same three GETs N times. A player
  sending their second list is already on that page.
* Login ran the warm-up walk, which lands on /dorf1.php, and then immediately
  fetched /dorf1.php again to read the gpack version out of it.

Both are duplicate requests no browser emits, and both burn the paced activity
budget for nothing.
"""

import asyncio
from types import SimpleNamespace

from travian_api.stealth.navigator import PageNavigator


class _FakePageState:
    """The ONE page field the real client keeps for the Referer.

    PageNavigator now derives "where am I?" from this rather than keeping its own
    copy, because the two could disagree: the real field is written by every page
    load, whoever triggered it, while the navigator's copy had a single writer. A
    double that omits it makes every chain look un-walked.
    """

    def __init__(self):
        self.last_page_path = None

    def update(self, path):
        self.last_page_path = path


class _RecordingHttp:
    def __init__(self):
        self.gets: list[str] = []
        self.browser_headers = _FakePageState()

    async def get_html(self, path, **kw):
        self.gets.append(path)
        self.browser_headers.update(path)
        return "<html></html>"


def _navigator():
    http = _RecordingHttp()
    # Delays are behaviour under test elsewhere; here they only slow the suite.
    nav = PageNavigator(http_client=http, human_delay=SimpleNamespace(wait=_no_wait), enabled=True)
    return nav, http


async def _no_wait(*a, **k):
    return None


class TestFarmListNavigation:
    def test_the_first_list_walks_the_full_chain(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        assert http.gets == [
            "/dorf2.php?newdid=40001",
            "/build.php?gid=16&tt=2&newdid=40001",
            "/build.php?gid=16&tt=99&newdid=40001",
        ], "the referer chain must be built truthfully the first time"

    def test_a_second_list_on_the_same_village_re_requests_nothing(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        http.gets.clear()

        asyncio.run(nav.navigate_to_farm_list(village_id=40001))

        assert http.gets == [], "already on this village's farm-list tab"

    def test_switching_village_walks_the_chain_again(self):
        # The context really did change, so the navigation is real.
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        http.gets.clear()

        asyncio.run(nav.navigate_to_farm_list(village_id=40002))

        assert http.gets == [
            "/dorf2.php?newdid=40002",
            "/build.php?gid=16&tt=2&newdid=40002",
            "/build.php?gid=16&tt=99&newdid=40002",
        ]

    def test_five_lists_on_one_village_cost_one_chain_not_five(self):
        nav, http = _navigator()
        for _ in range(5):
            asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        assert len(http.gets) == 3, f"expected one chain, got {http.gets}"


class TestRallyPointNavigation:
    def test_it_does_not_reload_the_rally_point_it_is_on(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_rally_point(village_id=40001))
        http.gets.clear()

        asyncio.run(nav.navigate_to_rally_point(village_id=40001))

        assert http.gets == []


class TestTheNavigatorCannotDisagreeWithTheWire:
    """The skip is only safe if "where am I?" matches what the Referer says.

    The navigator used to keep its own `_current_page`, written only by its own
    `_visit`. The field the Referer is built from is written by EVERY page load,
    and dozens of call sites reach `get_html` directly without passing through
    the navigator. So a concurrent operation could move the session while the
    navigator still believed it was on the farm-list tab -- and the skip would
    then send a farm-list POST refered from a page with no farm-list form, which
    is precisely the "impossible from a real browser" pairing the Referer work
    exists to prevent.

    The unconditional re-walk used to hide this. Removing it made the divergence
    matter, so the divergence had to go.
    """

    def test_a_page_load_from_outside_the_navigator_is_noticed(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        assert nav.current_page == "/build.php?gid=16&tt=99&newdid=40001"

        # Something else moves the session -- an oasis raider, a queue poll.
        asyncio.run(http.get_html("/dorf1.php?newdid=40002"))

        assert nav.current_page == "/dorf1.php?newdid=40002", (
            "the navigator must follow the wire, not its own last visit"
        )

    def test_it_re_walks_after_another_operation_moved_the_session(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_farm_list(village_id=40001))
        asyncio.run(http.get_html("/dorf1.php?newdid=40002"))
        http.gets.clear()

        asyncio.run(nav.navigate_to_farm_list(village_id=40001))

        assert http.gets == [
            "/dorf2.php?newdid=40001",
            "/build.php?gid=16&tt=2&newdid=40001",
            "/build.php?gid=16&tt=99&newdid=40001",
        ], "a session that moved must be navigated back, not assumed"

    def test_the_rally_point_skip_is_also_wire_derived(self):
        nav, http = _navigator()
        asyncio.run(nav.navigate_to_rally_point(village_id=40001))
        asyncio.run(http.get_html("/karte.php"))
        http.gets.clear()

        asyncio.run(nav.navigate_to_rally_point(village_id=40001))

        assert http.gets, "the session left the rally point, so it must be re-entered"
