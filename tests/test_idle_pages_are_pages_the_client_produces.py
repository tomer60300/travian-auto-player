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

import re

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
    # Linked by the live navigation, read 2026-09-15. Never opened in the
    # capture, which is why it was briefly dropped -- but a URL the game's own
    # markup links is a URL the client produces, and that is the real standard.
    "/profile",
    "/build.php?gid=19",
}

# Present in the codebase before the live page was read, absent from its
# markup. `/report/all` is the one that survived longest: the report tabs are
# /report, /report/overview, /report/offensive, /report/defensive,
# /report/scouting, /report/other, /report/archive and /report/surrounding, and
# pagination is a bare `?page=N`. There is no `all`.
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


class TestTheLocaleBundlesAreNotOurs:
    """Settled, so nobody re-treads it: we should NOT fetch the /js/ bundles.

    They are 1,265 of the capture's 1,631 requests -- 84% -- and we ask for
    none, which looked for a while like the largest single divergence on the
    branch, and like an easy win.

    It is neither. Playwright's recorder fires on cache hits, and reading the
    live page's own `performance` entries on 2026-09-15 settles what the
    recorder could not::

        /js/en-US/layout.json    transferSize=0  decodedBodySize=4169   1ms
        /js/en-US/plus.json      transferSize=0  decodedBodySize=48668  3ms
        ... twelve of them, every one transferSize=0

    `transferSize` 0 against a non-zero `decodedBodySize` is the definition of
    a cache hit. The player's browser fetched these once, weeks ago, and has
    touched the network for them zero times since. So on the wire -- which is
    the only place anyone is looking -- a real session and ours already agree:
    both ask for none.

    Fetching them would have taken us from matching the observation to being
    the only "browser" on the server with a permanently cold cache, at 12
    requests per page load against a shared daily activity ceiling. The
    intuition "84% of their traffic is missing from ours" was measuring the
    recorder, not the network.
    """

    # A bundle URL in a string literal, not the path written out in prose. The
    # capture's bundle list is quoted in comments, and describing a request is
    # not making one -- the same distinction the /videofeature/start guard draws.
    _ASKING_FOR_A_BUNDLE = re.compile(r"""["']/js/""")

    def test_we_ask_for_no_locale_bundles(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "travian_api"
        asks = [
            f"{path.relative_to(src)}:{n}"
            for path in src.rglob("*.py")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if self._ASKING_FOR_A_BUNDLE.search(line)
        ]
        assert not asks, f"locale bundles come from the browser cache, not the network: {asks}"
