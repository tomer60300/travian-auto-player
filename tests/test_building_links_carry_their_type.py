"""A building is addressed `id=<slot>&gid=<type>`; a resource field is not.

Read off a live account 2026-09-15, from the game's own navigation markup. The
village view links its buildings with the type always present::

    /build.php?id=22&gid=24
    /build.php?id=36&gid=20
    /build.php?id=39&gid=16

and dorf1 links the resource fields bare, all eighteen of them::

    /build.php?id=1 … /build.php?id=18

Two different shapes for two different things, and the game is consistent about
which is which. We sent the bare form for both, so every building this app ever
opened was addressed the way the game addresses a wheat field.

It answers either way, which is exactly why it survived: the server does not
need the gid to find the building, the markup simply always carries it. Nothing
failed, nothing was logged, and the URL was one no in-game link produces.

The gid costs no request. The village view the navigation already loads lists
every slot with its type, and `_visit` was returning that HTML and dropping it
-- the same thing `TradeRouteService` does for the marketplace slot. A caller
holding the gid can pass it and skip even the parse.
"""

import asyncio
from types import SimpleNamespace

from travian_api.stealth.human_delay import HumanDelay
from travian_api.stealth.navigator import PageNavigator

VILLAGE_VIEW = (
    "<html><body><div id='village_map'>"
    "<a href='/build.php?id=22' class='level gid24' data-gid='24' title='Town Hall Level 3'></a>"
    "<a href='/build.php?id=36' class='level gid20' data-gid='20' title='Barracks Level 5'></a>"
    "<a href='/build.php?id=25' class='emptyBuildingSlot'></a>"
    "</div></body></html>"
)


class _Http:
    def __init__(self, page: str = VILLAGE_VIEW):
        self.urls: list[str] = []
        self.page = page
        self.base_url = "https://example.invalid"
        self.browser_headers = SimpleNamespace(last_page_path=None)

    async def get_html(self, url, **kw):
        self.urls.append(url)
        self.browser_headers.last_page_path = url
        return self.page if url.startswith("/dorf2") else "<html/>"


def _nav(page: str = VILLAGE_VIEW):
    http = _Http(page)
    return PageNavigator(http, HumanDelay(enabled=False), enabled=True), http


class TestTheTypeComesFromThePageWeAlreadyLoaded:
    def test_a_building_is_opened_with_its_gid(self):
        nav, http = _nav()

        asyncio.run(nav.navigate_to_building(22, village_id=7))

        assert http.urls == ["/dorf2.php?newdid=7", "/build.php?id=22&gid=24&newdid=7"]

    def test_it_costs_no_extra_request(self):
        nav, http = _nav()

        asyncio.run(nav.navigate_to_building(36, village_id=7))

        assert len(http.urls) == 2

    def test_each_slot_gets_its_own_type(self):
        nav, http = _nav()

        asyncio.run(nav.navigate_to_building(36, village_id=7))

        assert http.urls[-1] == "/build.php?id=36&gid=20&newdid=7"

    def test_the_order_is_id_then_gid_then_the_village(self):
        nav, http = _nav()

        asyncio.run(nav.navigate_to_building(22, village_id=7))

        assert http.urls[-1] == "/build.php?id=22&gid=24&newdid=7"

    def test_a_caller_that_knows_the_type_is_believed(self):
        """The video-reward flow is handed the building id by its caller, so it
        passes it and the parse is skipped entirely."""
        nav, http = _nav(page="<html><body>unparseable</body></html>")

        asyncio.run(nav.navigate_to_building(22, village_id=7, gid=17))

        assert http.urls[-1] == "/build.php?id=22&gid=17&newdid=7"

    def test_the_type_survives_a_visit_that_skips_the_village_view(self):
        """A session already sitting on dorf2 -- another flow put it there --
        does not reload it, so there is no HTML to parse this time round. The
        gid has to come from what the last parse learned, or the skip silently
        costs the very thing the parse was for.
        """
        nav, http = _nav()
        asyncio.run(nav.navigate_to_building(22, village_id=7))
        http.browser_headers.last_page_path = "/dorf2.php?newdid=7"
        before = len(http.urls)

        asyncio.run(nav.navigate_to_building(36, village_id=7))

        assert http.urls[before:] == ["/build.php?id=36&gid=20&newdid=7"]

    def test_going_back_for_another_building_reloads_the_village_view(self):
        """And that is correct: a player returns to the village centre to click
        the next building, rather than teleporting between build pages."""
        nav, http = _nav()
        asyncio.run(nav.navigate_to_building(22, village_id=7))
        before = len(http.urls)

        asyncio.run(nav.navigate_to_building(36, village_id=7))

        assert http.urls[before:] == [
            "/dorf2.php?newdid=7",
            "/build.php?id=36&gid=20&newdid=7",
        ]


class TestUnknownStaysUnknown:
    def test_an_empty_slot_is_opened_bare(self):
        """An empty slot has no type to name. Inventing one would address some
        other building; the bare form is what the construction picker uses."""
        nav, http = _nav()

        asyncio.run(nav.navigate_to_building(25, village_id=7))

        assert http.urls[-1] == "/build.php?id=25&newdid=7"

    def test_an_unreadable_village_view_falls_back_rather_than_guessing(self):
        nav, http = _nav(page="<html><body>maintenance</body></html>")

        asyncio.run(nav.navigate_to_building(22, village_id=7))

        assert http.urls[-1] == "/build.php?id=22&newdid=7"

    def test_a_resource_field_is_still_addressed_bare(self):
        """dorf1 links all eighteen without a gid, so this shape is correct and
        must not drift toward the building one."""
        nav, http = _nav()

        asyncio.run(nav.navigate_to_resource_field(5, village_id=7))

        assert http.urls[-1] == "/build.php?id=5&newdid=7"
