"""Video reward service — watches an ad the way the game's client watches one.

The whole flow, recorded live on 2026-09-15 with the operator watching two ads
end to end (a building speed-up and a hero adventure), request and response
bodies included::

    GET  /api/v1/videofeature/open/<rewardType>      Referer: the offering page
      <- {"vrid":"ipqm…cG0","hash":null,"identifier":"75f8…0f1a",
          "gameId":"09ee…d311e4","suffix":""}

    GET  /fallback/v1/request-ad?game_id=<gameId>    Referer: /
      <- {"status":"success","data":{"video_src_url":"…/Hyperdrome_EN.mp4",
          "tracking_url":"…","token":"d471…b83e"}}
      (plus an ih.adscale.de/map iframe, which is the ad network's own pixel)

    … 23-34 seconds of video …

    POST /fallback/v1/reward                         Referer: /
      -> {"token":"d471…b83e","external_identifier":"75f8…0f1a","custom_1":"ipqm…cG0"}
      <- {"status":"success","data":{"rewarded":true,
          "conversionId":"724355b8-d589-45d3-9f63-7ae575f6c8db",
          "signature":"f15843e619164f612971fad8799c5d9671388a7a", …}}

    POST /api/v1/videofeature/ends                   Referer: the offering page
      -> {"vrid":"ipqm…cG0","hash":"724355b8-…-7ae575f6c8db" + "f15843e6…71388a7a"}
      <- {"token":"s60Vn0D4R7PLoZlr"}

Four things in that the previous implementation had wrong, and none of them
were small:

* **The ad provider moved.** This service talked to ATG -- fetched an iframe,
  decoded a base64 config, fired ``fc.php`` progress ticks every three seconds,
  then parsed a ``<sign>`` element out of an XML answer from ``xs.php``. None of
  that is in the flow any more. The ad leg is two requests to Travian's own
  ``/fallback/v1/`` and the network is adscale.
* **``open`` is a GET, and carries nothing.** We POSTed it with a body naming
  villageId/slotId/buildingId. The real request has no body and no query: the
  server takes the target from the session and the Referer. Which changes what a
  caller has to do -- see :meth:`_stand_where_the_offer_is`.
* **The claim hash is not computed.** It is ``conversionId`` and ``signature``
  from the ad network's receipt, concatenated, no separator. Verified on both
  watches.
* **There is no ``/api/v1/videofeature/start``.** Two complete granted claims
  and a 1,631-request session contain none. That one was removed first, on its
  own, because it was the only part that could be removed without knowing any
  of the rest.

The second-order win is that the whole thing now runs on the client that was
already open. There used to be a separate HTTP session for the ad host, and
keeping its identity coherent with the game's -- same impersonation target,
same client hints, no Travian cookies -- was a standing maintenance cost that
had already leaked a ``python-httpx`` User-Agent once. The ad host is Travian
now. That client is gone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any, Dict

from ..clients.http_client import HttpClient

logger = logging.getLogger(__name__)

# Video reward types
REWARD_TYPES = {
    "buildingUpgrade": "25% faster building construction",
    "productionBoost": "+15% resource production (8h)",
    "adventureDuration": "Reduced adventure travel time",
    "smithyUpgrade": "Reduced smithy research time",
    "academyResearch": "Reduced academy research time",
    "lumberProductionBonus": "+15% lumber production (8h)",
    "clayProductionBonus": "+15% clay production (8h)",
    "ironProductionBonus": "+15% iron production (8h)",
    "cropProductionBonus": "+15% crop production (8h)",
}

# Reward type -> the page whose markup carries the button, where one is known.
# Both recorded opens came from such a page, and the open call sends nothing
# else, so this Referer is the whole of the context the server gets.
#
# `buildingUpgrade` is absent on purpose: its page is per-building
# (/build.php?id=<slot>&gid=<gid>) and is built from the caller's parameters.
_OFFER_PAGE = {
    "adventureDuration": "/hero/adventures",
}


class VideoRewardResult:
    """Result of a video reward claim."""

    def __init__(self, success: bool, reward_type: str, message: str = "", raw: str = ""):
        self.success = success
        self.reward_type = reward_type
        self.message = message
        self.raw = raw

    def __repr__(self):
        return (
            f"VideoRewardResult(success={self.success}, "
            f"type={self.reward_type}, msg={self.message})"
        )


class VideoRewardService:
    """Claims video rewards by following the game's own ad flow.

    Five requests, all on Travian's host:

    1. ``GET  /api/v1/videofeature/open/{type}``  -> vrid, identifier, gameId
    2. ``GET  /fallback/v1/request-ad?game_id=``  -> ad token
    3. wait out the video
    4. ``POST /fallback/v1/reward``               -> conversionId + signature
    5. ``POST /api/v1/videofeature/ends``         -> the reward

    The module docstring carries the capture this is built from, byte for byte.
    """

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client

    async def close(self) -> None:
        """Nothing to close any more, and kept because callers call it.

        There used to be a second HTTP client here for the ad network, with its
        own cookie jar, its own impersonation target and its own lifecycle trap
        (httpx says ``aclose``, curl_cffi says ``close``; picking wrong leaked a
        session per claim). The ad network is on Travian's own host now, so the
        claim runs on the client that was already open.
        """

    @staticmethod
    def _open_endpoint(reward_type: str) -> tuple[str, str | None]:
        """The path segment for a reward type, and the resource it implies.

        The four per-resource bonuses are one ``productionBoost`` feature with a
        resource attached -- a distinction this service's callers make and the
        URL does not.
        """
        resource_map = {
            "lumberProductionBonus": ("productionBoost", "lumber"),
            "clayProductionBonus": ("productionBoost", "clay"),
            "ironProductionBonus": ("productionBoost", "iron"),
            "cropProductionBonus": ("productionBoost", "crop"),
            "productionBoost": ("productionBoost", None),
        }
        return resource_map.get(reward_type, (reward_type, None))

    @staticmethod
    def _watch_seconds() -> float:
        """How long to let the ad run before claiming.

        Two real watches, timed from the ad request to the reward POST: 33.6s
        and 23.2s. The spread is the ad itself -- the second was
        ``Hyperdrome_EN.mp4`` -- plus however long the player took to click
        after it finished. There is no single right number, and a constant would
        be the wrong SHAPE whatever its value: a reward flow that always takes
        exactly N seconds is a stronger signal than one that takes too long.

        Log-normal around 31s, floored at 24s, just above the shorter
        observation. Erring long is nearly free -- a player who glanced away
        mid-ad looks like a player -- while erring short is a claim for a video
        that had not finished.
        """
        return min(max(random.lognormvariate(math.log(31.0), 0.18), 24.0), 75.0)

    def _origin(self) -> str:
        return self.http_client.base_url.rstrip("/")

    async def _stand_where_the_offer_is(
        self, reward_type: str, extra_params: Dict[str, Any]
    ) -> str:
        """Navigate to the page offering this reward; return its absolute URL.

        Both recorded opens came from the page carrying the button -- a building
        speed-up from ``/build.php?id=22&gid=22``, an adventure from
        ``/hero/adventures``. The open call carries no body, so that Referer is
        the entire context the server has for deciding what is being sped up.

        Which makes this a correctness step before it is a stealth one. A
        building claim made from the wrong page is not a failed claim; it is a
        claim against whatever the session was last looking at, and the daily
        video is spent either way.

        A reward type whose page is not known falls back to wherever the session
        already is. That is not a guess at a URL, it is declining to make one --
        the same rule the navigator's page table follows.
        """
        origin = self._origin()
        navigator = getattr(self.http_client, "navigator", None)

        if reward_type == "buildingUpgrade":
            slot = extra_params.get("slotId")
            gid = extra_params.get("buildingId")
            village = extra_params.get("villageId")
            if slot is None or gid is None:
                raise ValueError("buildingUpgrade needs slotId and buildingId to find its page")
            # `newdid` last, as everywhere: see stealth/navigator.py.
            path = f"/build.php?id={slot}&gid={gid}"
            if village:
                path = f"{path}&newdid={village}"
            if navigator is not None and navigator.enabled:
                await navigator.navigate_to_building(int(slot), village)
            await self.http_client.get_html(path, skip_reauth=True)
            return f"{origin}{path}"

        known = _OFFER_PAGE.get(reward_type)
        if known:
            await self.http_client.get_html(known, skip_reauth=True)
            return f"{origin}{known}"

        current = self.http_client.browser_headers.last_page_path
        return f"{origin}{current}" if current else f"{origin}/dorf1.php"

    async def claim_reward(self, reward_type: str, **extra_params) -> VideoRewardResult:
        """Watch an ad the way the game's client watches one, and claim it.

        ``extra_params`` still accepts ``villageId``/``slotId``/``buildingId``,
        but they are no longer SENT -- the open call carries no body at all.
        They locate the page the request has to come from instead. The target
        used to be something we asserted; it is now something the session
        demonstrates.
        """
        if reward_type not in REWARD_TYPES:
            return VideoRewardResult(
                False, reward_type, f"Unknown reward type. Valid: {', '.join(REWARD_TYPES.keys())}"
            )

        endpoint, resource = self._open_endpoint(reward_type)
        origin = self._origin()

        try:
            referer = await self._stand_where_the_offer_is(reward_type, extra_params)

            open_path = f"/api/v1/videofeature/open/{endpoint}"
            if resource:
                # Carried over from the previous implementation and flagged as
                # the one unverified piece of this flow: the two recorded opens
                # were `buildingUpgrade` and `adventureDuration`, neither of
                # which names a resource, so how a production boost says which
                # resource it wants has not been observed.
                open_path = f"{open_path}?resource={resource}"
            logger.info("Opening a video session for %s", reward_type)
            opened = await self.http_client.get_json(
                open_path, referer=referer, skip_reauth=True, safe_to_retry=False
            )

            vrid = opened.get("vrid") if isinstance(opened, dict) else None
            identifier = opened.get("identifier") if isinstance(opened, dict) else None
            game_id = opened.get("gameId") if isinstance(opened, dict) else None
            if not (vrid and identifier and game_id):
                return VideoRewardResult(
                    False, reward_type, f"Open did not hand back a session: {opened}", str(opened)
                )

            # Referred from the site root, not the game page: this is the ad
            # frame asking, and the frame's own document is the root.
            ad = await self.http_client.get_json(
                f"/fallback/v1/request-ad?game_id={game_id}",
                referer=f"{origin}/",
                skip_reauth=True,
            )
            token = (ad.get("data") or {}).get("token") if isinstance(ad, dict) else None
            if not token:
                return VideoRewardResult(False, reward_type, f"No ad was offered: {ad}", str(ad))

            watching = self._watch_seconds()
            logger.info(
                "Watching the ad for %.0fs before claiming — the reward is granted for a "
                "video that finished, so the wait is the feature, not overhead.",
                watching,
            )
            await asyncio.sleep(watching)

            rewarded = await self.http_client.post_json(
                "/fallback/v1/reward",
                {"token": token, "external_identifier": identifier, "custom_1": vrid},
                referer=f"{origin}/",
                skip_reauth=True,
                safe_to_retry=False,
            )
            receipt = (rewarded.get("data") or {}) if isinstance(rewarded, dict) else {}
            conversion_id = receipt.get("conversionId")
            signature = receipt.get("signature")
            if not receipt.get("rewarded") or not (conversion_id and signature):
                return VideoRewardResult(
                    False,
                    reward_type,
                    f"The ad network did not grant a reward: {rewarded}",
                    str(rewarded),
                )

            # The hash is the two halves of that receipt, concatenated, with no
            # separator and no transformation. Verified on both watches:
            #
            #   c0069958-…-2cfda24e4da2 + 7e5e7cc8…97762333
            #   724355b8-…-7ae575f6c8db + f15843e6…71388a7a
            #
            # The previous implementation went looking for a <sign> element in
            # an XML answer from a provider no longer in this flow.
            claim_hash = f"{conversion_id}{signature}"

            ends_data = await self.http_client.post_json(
                "/api/v1/videofeature/ends",
                {"vrid": vrid, "hash": claim_hash},
                referer=referer,
                skip_reauth=True,
                safe_to_retry=False,
            )

            if not isinstance(ends_data, dict) or "response_text" in ends_data:
                # post_json hands back {"response_text": ...} for a body that
                # was not JSON (an HTML soft-block, a maintenance page), and a
                # non-dict at all for some other JSON shape (e.g. a bare
                # array). Neither names a refusal -- ends_data.get("error")
                # is simply absent on the first, and raises AttributeError on
                # the second -- so both used to fall through to (or crash
                # past) the success return below. This POST already went out
                # and is non-idempotent, so the honest verdict is the one
                # this codebase gives every other write's unreadable answer
                # (farm_list_service._check_mutation,
                # trade_route_service.ToggleResponseUnreadable): unverified,
                # never a silent success.
                return VideoRewardResult(
                    False,
                    reward_type,
                    "Claim answered with a body the game's own shape is not in, so "
                    "whether the reward was granted cannot be read; it may already "
                    "have taken effect",
                    str(ends_data),
                )

            if ends_data.get("error"):
                return VideoRewardResult(
                    False,
                    reward_type,
                    f"Server rejected: {ends_data.get('error')} - {ends_data.get('message', '')}",
                    str(ends_data),
                )

            # For buildingUpgrade: follow the redirectTo URL to actually start
            # the build. Both recorded claims answered with a token and nothing
            # else, so this is conditional on the field being present rather
            # than expected -- and the field, when it comes, is a link the page
            # rendered, which is why it is followed rather than reconstructed.
            redirect_to = ends_data.get("redirectTo")
            if redirect_to and reward_type == "buildingUpgrade":
                logger.info("Following the buildingUpgrade redirect: %s", redirect_to)
                await self.http_client.get_html(redirect_to, skip_reauth=True, safe_to_retry=False)

            logger.info("Reward claimed. Type: %s", reward_type)
            return VideoRewardResult(
                True,
                reward_type,
                f"Reward claimed: {REWARD_TYPES.get(reward_type, reward_type)}",
                str(ends_data),
            )

        except Exception as e:
            logger.error("Video reward failed: %s", e)
            return VideoRewardResult(False, reward_type, f"Error: {e}")

    async def get_available_rewards(self) -> Dict[str, bool]:
        """
        Check which video rewards are currently available.

        Returns:
            Dict mapping reward type to availability
        """
        try:
            resp = await self.http_client.post_json(
                "/api/v1/graphql",
                {
                    "query": """{ ownPlayer { productionBoost {
                    lumber { videoFeatureAvailable isActive }
                    clay { videoFeatureAvailable isActive }
                    iron { videoFeatureAvailable isActive }
                    crop { videoFeatureAvailable isActive }
                } } }"""
                },
            )
            data = resp.get("data", {}).get("ownPlayer", {}).get("productionBoost", {})
            result = {}
            for resource in ["lumber", "clay", "iron", "crop"]:
                info = data.get(resource, {})
                result[f"{resource}ProductionBonus"] = info.get("videoFeatureAvailable", False)
                if info.get("isActive"):
                    result[f"{resource}_active"] = True
            return result
        except Exception as e:
            logger.warning(f"Failed to check rewards: {e}")
            return {}
