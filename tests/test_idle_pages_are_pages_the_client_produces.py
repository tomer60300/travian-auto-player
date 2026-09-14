"""Every idle page we browse must be a URL the game's own client emits.

The strongest bot signal is not timing. It is asking for something no client
asks for. A deprecated-path trap costs the server one route and catches every
automation built from a wiki page or an old fork, because those reach for
``/statistiken.php`` long after the gpack stopped linking it -- and no amount
of log-normal jitter takes that request back.

We were reaching for two: ``/statistiken.php`` and ``/spieler.php``, the GERMAN
route names, on an English server. The 2026-09-15 capture -- 1,631 requests,
one player, half an hour -- contains neither, and contains one visit to
``/statistics``, which is what this gpack links.

So this file is the fence around that mistake. The idle pages are exactly the
top-level pages the capture observed, at the URLs it observed, and both callers
(the navigator's warm-up/idle browse and the oasis sweep's noisy sleep) draw
from the one table.
"""

from travian_api.services.oasis_raider_service import _NOISE_PAGES
from travian_api.stealth.navigator import (
    _WARMUP_PAGE_AFFINITY,
    _WARMUP_PAGE_DESC,
    _WARMUP_PAGES,
    PAGE_PATHS,
    PageNavigator,
)

# Read straight off the capture's document loads. The whole point is that this
# list is observation, so it is written out rather than derived from the code
# under test.
OBSERVED = {
    "/dorf1.php",
    "/dorf2.php",
    "/karte.php",
    "/report",
    "/statistics",
    "/build.php?gid=19",
}

# Present in the codebase before the capture, absent from all 1,631 requests.
NEVER_OBSERVED = {"/statistiken.php", "/spieler.php", "/profile.php", "/report/all"}


class TestTheTableIsTheCapture:
    def test_every_page_we_browse_was_observed(self):
        assert set(PAGE_PATHS.values()) == OBSERVED

    def test_no_page_the_client_never_asked_for_survives(self):
        for caller in (set(PAGE_PATHS.values()), set(_NOISE_PAGES)):
            assert not caller & NEVER_OBSERVED

    def test_the_two_callers_share_one_table(self):
        """A second list is a list that drifts, and this one had."""
        assert set(_NOISE_PAGES) == set(PAGE_PATHS.values())

    def test_every_page_has_a_weight_and_something_to_say(self):
        assert set(_WARMUP_PAGE_AFFINITY) == set(_WARMUP_PAGES)
        assert set(_WARMUP_PAGE_DESC) == set(_WARMUP_PAGES)


class TestTheUrlComesFromTheTableNotFromTheName:
    """``f"/{page}.php"`` is what put the German routes on the wire.

    The rule quietly asserted that every page is ``<name>.php``. Two of this
    gpack's are extensionless and one is a query on build.php, so a name-derived
    URL cannot be right for them however the name is spelled.
    """

    def test_an_extensionless_page_keeps_its_shape(self):
        assert PageNavigator._warmup_page_path("statistics", "?newdid=7") == "/statistics"

    def test_a_query_string_page_keeps_its_query(self):
        assert PageNavigator._warmup_page_path("troops", "?newdid=7") == "/build.php?gid=19"

    def test_only_village_scoped_pages_take_the_selector(self):
        # The global pages have no village context to pin, and appending one
        # would produce a URL the client does not emit -- the same class of
        # mistake, arrived at from the other direction.
        for page in PAGE_PATHS:
            path = PageNavigator._warmup_page_path(page, "?newdid=7")
            assert ("newdid" in path) == (path.startswith(("/dorf1", "/dorf2", "/karte")))

    def test_a_visited_path_maps_back_to_its_page(self):
        for page, path in PAGE_PATHS.items():
            assert PageNavigator._page_key(path) == page

    def test_a_village_scoped_path_maps_back_too(self):
        assert PageNavigator._page_key("/dorf1.php?newdid=7") == "dorf1"

    def test_another_build_page_is_not_the_troop_overview(self):
        # /build.php with the query stripped is not a page at all; treating it
        # as one would call every marketplace and rally point "troops" and let
        # the idle walk claim it was already there.
        assert PageNavigator._page_key("/build.php?gid=17&t=3") is None
        assert PageNavigator._page_key("/build.php") is None
