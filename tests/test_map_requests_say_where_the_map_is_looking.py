"""A map XHR's Referer names a coordinate, and it moves as the sweep moves.

Tile popups and pans are fired by the map page's own JS, so their Referer is
whatever the address bar says -- and the address bar tracks the VIEWPORT, which
moves as the player scrolls. From the 2026-09-15 capture::

    148  POST /api/v1/map/position      <- /karte.php
    150  POST /api/v1/map/position      <- /karte.php?zoom=1&x=20&y=89
    151  POST /api/v1/map/tile-details  <- /karte.php?zoom=1&x=15&y=90
    535  POST /api/v1/map/tile-details  <- /karte.php?zoom=1&x=11&y=82
    550  POST /api/v1/map/tile-details  <- /karte.php?x=10&y=81

Ours said ``/karte.php``. Bare, and the SAME bare string, for every request in
a sweep -- and an oasis sweep or a scout scan is hundreds of tile requests. So
the account emitted hundreds of map XHRs whose Referer never once named a
coordinate, on a page whose whole purpose is to be at a coordinate.

That needs no timing analysis to catch. It is one string comparison, on a
header we were already sending, and it is constant across every account we run
and every sweep they ever do.
"""

import re
from types import SimpleNamespace

from travian_api.stealth.human_delay import HumanDelay
from travian_api.stealth.navigator import PageNavigator, map_viewport_referer

BASE = "https://example.invalid"
VIEWPORT = re.compile(r"^https://example\.invalid/karte\.php(\?(zoom=1&)?x=-?\d+&y=-?\d+)?$")


def _navigator(identity: str = "acct-a", on: str = "/karte.php") -> PageNavigator:
    """A navigator whose session is ON the map, which is the precondition for a
    map Referer to be true at all -- see `TestItWillNotClaimAPageWeAreNotOn`."""
    nav = PageNavigator(
        SimpleNamespace(base_url=BASE, browser_headers=SimpleNamespace(last_page_path=on)),
        HumanDelay(enabled=False),
        enabled=True,
    )
    nav.seed_routes(identity)
    return nav


class TestTheRefererFollowsTheViewport:
    def test_the_first_request_after_opening_the_map_is_bare(self):
        """Capture line 148. The map has not been moved yet, so neither has the
        address bar."""
        assert _navigator().map_viewport_referer(10, 81) == f"{BASE}/karte.php"

    def test_the_next_one_names_where_the_map_already_was(self):
        nav = _navigator()
        nav.map_viewport_referer(10, 81)

        assert nav.map_viewport_referer(11, 82).endswith("x=10&y=81")

    def test_it_walks_rather_than_repeating(self):
        """The property the old behaviour failed: a sweep's referers are a
        sequence, not one constant repeated."""
        nav = _navigator()
        walk = [nav.map_viewport_referer(x, 88) for x in range(20, 32)]

        assert len(set(walk)) == len(walk)

    def test_every_referer_is_a_shape_the_capture_contains(self):
        for identity in ("acct-a", "acct-b", "acct-c", "acct-d"):
            nav = _navigator(identity)
            for x in range(5):
                assert VIEWPORT.match(nav.map_viewport_referer(x, 70)), identity

    def test_negative_coordinates_survive(self):
        # Travian maps are centred on (0,0), so half the world is negative and
        # a regex or format that quietly dropped the sign would be wrong for
        # half of every account's raiding.
        nav = _navigator()
        nav.map_viewport_referer(-12, -3)

        assert nav.map_viewport_referer(0, 0).endswith("x=-12&y=-3")

    def test_reopening_the_map_resets_the_address_bar(self):
        """A fresh page load is a fresh address bar, so the lag restarts."""
        import asyncio

        visited: list[str] = []

        class _Http:
            base_url = BASE
            browser_headers = SimpleNamespace(last_page_path=None)

            async def get_html(self, path, **kw):
                visited.append(path)
                self.browser_headers.last_page_path = path
                return "<html/>"

        nav = PageNavigator(_Http(), HumanDelay(enabled=False), enabled=True)
        nav.map_viewport_referer(10, 81)
        nav.map_viewport_referer(11, 82)

        asyncio.run(nav.navigate_to_map())

        assert visited == ["/karte.php"]
        assert nav.map_viewport_referer(12, 83) == f"{BASE}/karte.php"


class TestTheFormIsPerAccountNotPerFleet:
    """Both viewport URL forms are in the capture; picking one for everyone
    would put an identical string on every map request every account makes."""

    def _form(self, identity: str) -> str:
        nav = _navigator(identity)
        nav.map_viewport_referer(1, 1)
        return nav.map_viewport_referer(2, 2)

    def test_it_is_stable_across_restarts(self):
        assert self._form("acct-a") == self._form("acct-a")

    def test_the_population_writes_both_forms(self):
        forms = {"zoom=1" in self._form(f"acct-{i}") for i in range(20)}

        assert forms == {True, False}


class TestTheHelperAsksTheClientThatWillSend:
    """Several map XHRs go through the RECON client, whose navigator is a
    different object with its own viewport. A Referer taken from the primary
    would describe a session that did not send the request."""

    def test_it_uses_the_given_clients_own_navigator(self):
        primary = SimpleNamespace(navigator=_navigator("primary"))
        recon = SimpleNamespace(navigator=_navigator("recon"))

        map_viewport_referer(primary, 10, 81)
        map_viewport_referer(primary, 11, 82)
        # The recon client has moved nowhere, so its first call is still bare.
        assert map_viewport_referer(recon, 30, 30) == f"{BASE}/karte.php"

    def test_a_client_with_no_navigator_pins_nothing(self):
        assert map_viewport_referer(SimpleNamespace(), 1, 2) is None

    def test_a_disabled_navigator_pins_nothing(self):
        nav = _navigator()
        nav.enabled = False

        assert map_viewport_referer(SimpleNamespace(navigator=nav), 1, 2) is None


class TestEveryMapCallSiteIsPinned:
    """A source scan, because the failure mode is a call site that forgets.

    The old behaviour was not a bug in any one of these -- it was six call
    sites each correctly inheriting an account-wide field that happened to say
    the same thing every time.
    """

    def test_no_map_request_is_left_unpinned(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "travian_api"
        unpinned = []
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for call in re.finditer(
                r'post_json\(\s*\n?\s*"/api/v1/map/(?:position|tile-details)".*?\n\s*\)',
                text,
                re.S,
            ):
                if "referer=" not in call.group(0):
                    line = text[: call.start()].count("\n") + 1
                    unpinned.append(f"{path.relative_to(src)}:{line}")
        assert not unpinned, "map XHRs with no viewport Referer:\n" + "\n".join(unpinned)


class TestItWillNotClaimAPageWeAreNotOn:
    """The pin has to be TRUE, which is the whole argument for pinning at all.

    Not every caller of the tile APIs opens the map first. `target_resolver`
    resolves a coordinate straight from wherever the session happens to be --
    a village overview, a rally point, a report. Pinning `/karte.php?x=..&y=..`
    there would state that the request came from a page this session never
    loaded, which is a worse lie than the constant `/karte.php` it replaced,
    and a self-inflicted one.

    None means "inherit", and what gets inherited is the page we are actually
    on.
    """

    def test_a_session_that_never_opened_the_map_pins_nothing(self):
        nav = _navigator(on="/dorf1.php")

        assert nav.map_viewport_referer(10, 81) is None

    def test_a_session_that_has_not_loaded_anything_pins_nothing(self):
        nav = _navigator(on=None)

        assert nav.map_viewport_referer(10, 81) is None

    def test_navigating_away_mid_sweep_stops_the_claim(self):
        """A farm loop taking a page load during our pacing delay moves the
        session off the map. The next tile request must notice."""
        nav = _navigator()
        nav.map_viewport_referer(10, 81)
        nav._http.browser_headers.last_page_path = "/dorf2.php"

        assert nav.map_viewport_referer(11, 82) is None

    def test_coming_back_to_the_map_starts_the_walk_over(self):
        """The viewport was dropped when we left, so the first request after
        returning is referred from the bare page -- which is exactly what the
        capture's first map XHR does."""
        nav = _navigator()
        nav.map_viewport_referer(10, 81)
        nav._http.browser_headers.last_page_path = "/dorf2.php"
        nav.map_viewport_referer(11, 82)
        nav._http.browser_headers.last_page_path = "/karte.php"

        assert nav.map_viewport_referer(12, 83) == f"{BASE}/karte.php"

    def test_a_map_page_with_a_viewport_already_on_it_counts(self):
        nav = _navigator(on="/karte.php?x=19&y=88")

        assert nav.map_viewport_referer(10, 81) == f"{BASE}/karte.php"
