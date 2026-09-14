"""A scraped action URL goes back to the server exactly as it arrived.

Travian's build/upgrade links carry a ``checksum`` the server computed over the
exact query string it emitted. A browser can only follow such a link; it has no
mechanism for adding a parameter to it. So every edit we make to one -- appending
``&newdid``, appending ``&buildmaster``, stripping ``&buildmaster`` to
manufacture the free link out of the gold one -- produces a URL the server never
issued, and the server can recognise that by simply re-computing the checksum
over what it received. It is a single-request, zero-false-positive bot tell, and
no amount of TLS or header work hides it.

These tests pin the rule at both ends: the parser keeps the page's two links
apart instead of deriving one from the other, and the service sends whichever one
it chose untouched.
"""

import asyncio
from types import SimpleNamespace

from travian_api.models.buildings import BuildingDetail
from travian_api.parsers.html_parser import parse_build_page
from travian_api.services.building_service import BuildingService

CHECKSUM_FREE = "a1b2c3"
CHECKSUM_GOLD = "d4e5f6"

FREE_URL = f"/build.php?id=27&gid=17&action=build&checksum={CHECKSUM_FREE}"
GOLD_URL = f"/build.php?id=27&gid=17&action=build&buildmaster&checksum={CHECKSUM_GOLD}"


def _page(*urls: str) -> str:
    """A build page offering the given action links, as the game renders them."""
    buttons = "".join(
        f"""<button class="textButtonV1 green build"
               onclick="window.location.href = '{url}'">Upgrade</button>"""
        for url in urls
    )
    return f"""<html><body>
        <h1 class="titleInHeader">Marketplace <span class="level">Level 5</span></h1>
        <div id="contract">{buttons}</div>
    </body></html>"""


class TestTheParserKeepsTheTwoLinksApart:
    def test_both_links_are_reported_verbatim(self):
        detail = parse_build_page(_page(FREE_URL, GOLD_URL), slot_id=27)
        assert detail.upgrade_url == FREE_URL
        assert detail.gold_upgrade_url == GOLD_URL

    def test_a_gold_only_page_does_not_yield_a_synthesised_free_link(self):
        # Stripping "&buildmaster" would leave the GOLD checksum on a URL that
        # no longer matches it -- a mismatch the server computes for free.
        detail = parse_build_page(_page(GOLD_URL), slot_id=27)
        assert detail.gold_upgrade_url == GOLD_URL
        assert detail.upgrade_url == "", "a free link must not be manufactured"

    def test_a_free_only_page_offers_no_gold_link(self):
        detail = parse_build_page(_page(FREE_URL), slot_id=27)
        assert detail.upgrade_url == FREE_URL
        assert detail.gold_upgrade_url == "", "appending &buildmaster would break the checksum"


class _RecordingClient:
    """Captures the URL the service actually requests."""

    def __init__(self):
        self.requested: list[str] = []
        self.stealth_enabled = False
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        # The build flow holds this across scrape-then-act, because the scraped
        # action url cannot carry `newdid` and so depends on the session still
        # being on the village it was scraped from.
        self.village_context_lock = asyncio.Lock()

    async def get_html(self, url, **kw):
        self.requested.append(url)
        return "<html><body>Construction of Marketplace</body></html>"


class TestTheServiceSendsWhatThePageGave:
    def _service(self, detail: BuildingDetail):
        client = _RecordingClient()
        service = BuildingService(http_client=client)

        async def _detail(slot_id, village_id=None):
            return detail

        service.get_building_detail = _detail
        # Nothing must be inferred from a queue read in these tests.
        service.get_construction_queue = lambda *a, **k: _none()
        return service, client

    def _detail(self, **kw):
        return BuildingDetail(
            slot_id=27,
            gid=17,
            name="Marketplace",
            level=5,
            checksum=CHECKSUM_FREE,
            **kw,
        )

    def test_the_free_link_is_sent_unmodified_even_with_a_village_id(self):
        # The village is already active: get_building_detail fetched the page
        # with newdid=<village>. Appending it again to the checksummed link
        # would be both redundant and detectable.
        detail = self._detail(upgrade_url=FREE_URL, gold_upgrade_url=GOLD_URL)
        service, client = self._service(detail)

        asyncio.run(service.upgrade_building(27, village_id=40001, allow_gold=False))

        assert client.requested == [FREE_URL]
        sent = client.requested[0]
        assert "newdid" not in sent
        assert "buildmaster" not in sent

    def test_gold_uses_the_pages_own_gold_link_not_an_edited_free_one(self):
        detail = self._detail(upgrade_url=FREE_URL, gold_upgrade_url=GOLD_URL)
        service, client = self._service(detail)

        asyncio.run(service.upgrade_building(27, village_id=40001, allow_gold=True))

        assert client.requested == [GOLD_URL]
        assert CHECKSUM_GOLD in client.requested[0], "the gold link's own checksum"

    def test_a_missing_link_is_an_error_not_a_constructed_url(self):
        # The old code fell back to building "/dorf2.php?...&checksum=<free>"
        # from parts. That URL's checksum was never issued for that query string.
        detail = self._detail(upgrade_url="", gold_upgrade_url=GOLD_URL)
        service, client = self._service(detail)

        # upgrade_building reports failure in its result rather than raising.
        result = asyncio.run(service.upgrade_building(27, village_id=40001, allow_gold=False))

        assert result.success is False
        assert "no free upgrade link" in result.raw_response
        assert client.requested == [], "nothing is sent when there is no link to follow"


async def _none():
    return None


class TestTheAdClientIsGone:
    """The video-reward flow used to open a SECOND HTTP client for the ad host.

    Deliberately, so Travian's session cookies never reached that host -- and
    the cost was a whole second identity to keep coherent with the first. This
    class used to pin five properties of it, each one a bug that had already
    happened: it announced ``User-Agent: python-httpx/<version>`` in cleartext;
    it wore Chrome headers over Python TLS, the one combination HttpClient
    refuses to run at all; it could drift to a different impersonation target
    than the game client, which is two Chrome fingerprints from one account;
    and it had a closer that leaked a session per claim if you called the wrong
    one.

    All five are now unreachable, because there is no second client. The ad
    network moved onto Travian's own host (``/fallback/v1/``), recorded live
    2026-09-15, so the claim runs on the session that was already open and
    inherits its identity by construction rather than by copying.

    The strongest form of "these headers cannot be wrong" is not a test. It is
    having nowhere for them to be wrong.
    """

    def test_there_is_no_second_client_to_get_wrong(self):
        from travian_api.services.video_reward_service import VideoRewardService

        assert not hasattr(VideoRewardService, "_get_atg_client")
        assert not hasattr(VideoRewardService, "_extract_atg_config")

    def test_closing_the_service_is_a_no_op_that_still_answers(self):
        """Callers call it in a `finally`; it must stay callable."""
        from travian_api.services.video_reward_service import VideoRewardService

        svc = VideoRewardService(http_client=object())
        assert asyncio.run(svc.close()) is None

    def test_the_ad_leg_is_on_travians_own_host(self):
        import inspect

        from travian_api.services.video_reward_service import VideoRewardService

        # The claim itself, not the module: the module docstring names ATG's
        # fc.php and xs.php on purpose, recording what this used to do and why
        # it stopped. Prose about a dead provider is the point; a call to one
        # is the defect.
        claim = inspect.getsource(VideoRewardService.claim_reward)
        assert '"/fallback/v1/reward"' in claim
        assert "request-ad?game_id=" in claim
        assert "fc.php" not in claim
        assert "xs.php" not in claim

    def test_the_service_no_longer_imports_a_second_transport(self):
        from travian_api.services import video_reward_service

        assert not hasattr(video_reward_service, "httpx")
        assert not hasattr(video_reward_service, "_CurlAsyncSession")


class TestTheVillageCannotChangeUnderAScrapeThenAct:
    """A scraped action url cannot carry `newdid`, so it depends on the session
    still being on the village it was scraped from.

    Removing the appended `newdid` closed a real fingerprint (the server
    checksums the exact query string it emitted, so editing it is detectable) but
    moved village selection into shared session state with nothing serialising
    it. One HttpClient is shared by every service, and several switch villages
    with a bare `?newdid=` GET. A switch landing between the scrape and the
    action applies the build to the WRONG village -- a correctness bug, not a
    tell.
    """

    def test_the_build_flow_holds_the_account_wide_village_lock(self):
        client = _RecordingClient()
        service = BuildingService(http_client=client)
        held: list[bool] = []

        async def _detail(slot_id, village_id=None):
            # Observed from inside the sequence: the lock must already be held
            # here, or another operation could move the village before the
            # action url below is requested.
            held.append(client.village_context_lock.locked())
            return BuildingDetail(
                slot_id=27,
                gid=17,
                name="Marketplace",
                level=5,
                checksum=CHECKSUM_FREE,
                upgrade_url=FREE_URL,
            )

        service.get_building_detail = _detail
        asyncio.run(service.upgrade_building(27, village_id=40001))

        assert held == [True], "the scrape and the action must be one atomic step"

    def test_two_builds_cannot_interleave_their_scrape_and_action(self):
        client = _RecordingClient()
        service = BuildingService(http_client=client)
        order: list[str] = []

        async def _detail(slot_id, village_id=None):
            order.append(f"scrape-{slot_id}")
            await asyncio.sleep(0)  # a real await, where a switch could land
            order.append(f"scraped-{slot_id}")
            return BuildingDetail(
                slot_id=slot_id,
                gid=17,
                name="Marketplace",
                level=5,
                checksum=CHECKSUM_FREE,
                upgrade_url=FREE_URL,
            )

        service.get_building_detail = _detail

        async def _both():
            await asyncio.gather(
                service.upgrade_building(27, village_id=40001),
                service.upgrade_building(28, village_id=40002),
            )

        asyncio.run(_both())

        # Different slots AND different villages, so the slot lock does not
        # serialise them -- only the village lock does.
        assert order in (
            ["scrape-27", "scraped-27", "scrape-28", "scraped-28"],
            ["scrape-28", "scraped-28", "scrape-27", "scraped-27"],
        ), f"the two sequences interleaved: {order}"
