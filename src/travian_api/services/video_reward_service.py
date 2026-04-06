"""Video reward service — simulates video playback via ATG ad provider APIs."""
from __future__ import annotations

import asyncio
import json
import re
import base64
import logging
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlencode

import httpx

from ..clients.http_client import HttpClient
from ..exceptions import TravianError

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


def _jquery_param(obj: Any, prefix: str = '') -> List[Tuple[str, str]]:
    """Emulate jQuery.param() — serialize nested dicts to URL-encoded key=value pairs."""
    parts: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            full_key = f"{prefix}[{key}]" if prefix else str(key)
            if isinstance(value, dict):
                parts.extend(_jquery_param(value, full_key))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    parts.extend(_jquery_param({str(i): item}, full_key))
            else:
                parts.append((full_key, str(value)))
    else:
        parts.append((prefix, str(obj)))
    return parts


class VideoRewardResult:
    """Result of a video reward claim."""

    def __init__(self, success: bool, reward_type: str, message: str = "", raw: str = ""):
        self.success = success
        self.reward_type = reward_type
        self.message = message
        self.raw = raw

    def __repr__(self):
        return f"VideoRewardResult(success={self.success}, type={self.reward_type}, msg={self.message})"


class VideoRewardService:
    """
    Claims video rewards by simulating the ATG ad provider protocol.
    
    Flow:
    1. POST /api/v1/videofeature/open/{type} → vrid + iframe URL
    2. Fetch iframe HTML → extract ATG config (xsign with fc/xs URLs + xc state)
    3. Rapid-fire progress ticks to fc.php (simulates 30s video)
    4. POST xs.php → get signature hash
    5. POST /api/v1/videofeature/start → notify server
    6. POST /api/v1/videofeature/ends → claim reward with hash
    """

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
        # Separate httpx client for ATG requests (no Travian cookies/headers)
        self._atg_client: Optional[httpx.AsyncClient] = None

    async def _get_atg_client(self) -> httpx.AsyncClient:
        if not self._atg_client:
            self._atg_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        return self._atg_client

    async def close(self):
        if self._atg_client:
            await self._atg_client.aclose()
            self._atg_client = None

    async def claim_reward(
        self,
        reward_type: str,
        tick_delay_ms: int = 3000,
        wait_before_claim_s: float = 1.0,
        **extra_params,
    ) -> VideoRewardResult:
        """
        Claim a video reward by simulating ATG ad playback.
        
        Takes ~33 seconds (real 3s timing required by ATG server).
        
        Args:
            reward_type: One of the REWARD_TYPES keys (e.g. "ironProductionBonus")
            tick_delay_ms: Delay between progress ticks (ms). 3000ms required for signature.
            wait_before_claim_s: Wait time before calling /ends after getting hash
            
        Returns:
            VideoRewardResult
        """
        if reward_type not in REWARD_TYPES:
            return VideoRewardResult(
                False, reward_type,
                f"Unknown reward type. Valid: {', '.join(REWARD_TYPES.keys())}"
            )

        try:
            # Phase 1: Open video session
            logger.info(f"Opening video session for {reward_type}")
            # Build request data based on reward type
            open_body: Dict[str, Any] = {}
            open_endpoint = reward_type
            
            # Production boost types need resource parameter
            resource_map = {
                "lumberProductionBonus": ("productionBoost", "lumber"),
                "clayProductionBonus": ("productionBoost", "clay"),
                "ironProductionBonus": ("productionBoost", "iron"),
                "cropProductionBonus": ("productionBoost", "crop"),
                "productionBoost": ("productionBoost", None),  # needs resource param
            }
            
            if reward_type in resource_map:
                endpoint, resource = resource_map[reward_type]
                open_endpoint = endpoint
                if resource:
                    open_body["resource"] = resource
            elif reward_type == "buildingUpgrade":
                # buildingUpgrade requires villageId, slotId, buildingId
                for key in ("villageId", "slotId", "buildingId"):
                    if key in extra_params:
                        open_body[key] = extra_params[key]
                if not all(k in open_body for k in ("villageId", "slotId", "buildingId")):
                    return VideoRewardResult(
                        False, reward_type,
                        "buildingUpgrade requires villageId, slotId, buildingId params"
                    )
            
            open_data = await self.http_client.post_json(
                f"/api/v1/videofeature/open/{open_endpoint}", open_body,
                skip_reauth=True,
            )

            vrid = open_data.get("vrid") if isinstance(open_data, dict) else None
            iframe_url = open_data.get("videoIframeUrl") if isinstance(open_data, dict) else None

            if not vrid or not iframe_url:
                error_msg = open_data.get("error", open_data.get("message", "Unknown"))
                return VideoRewardResult(False, reward_type, f"Open failed: {error_msg}", str(open_data))

            logger.info(f"Got vrid={vrid}, iframe URL length={len(iframe_url)}")

            # Phase 2: Fetch iframe and extract ATG config
            atg_config = await self._extract_atg_config(iframe_url)
            if not atg_config:
                return VideoRewardResult(False, reward_type, "Failed to extract ATG config from iframe")

            xsign = atg_config.get("xsign")
            if not xsign:
                return VideoRewardResult(False, reward_type, "No xsign in ATG config")

            fc_url = xsign.get("fc")
            xs_url = xsign.get("xs")
            xc = xsign.get("xc")

            if not all([fc_url, xs_url, xc]):
                return VideoRewardResult(False, reward_type, f"Missing ATG fields: fc={bool(fc_url)} xs={bool(xs_url)} xc={bool(xc)}")

            # Get banner/zone IDs
            waterfall = atg_config.get("waterfall", [])
            bid = waterfall[0].get("bid", "17606") if waterfall else "17606"
            zone_id = str(atg_config.get("zone_id", "3716"))

            logger.info(f"ATG config: fc={fc_url[:50]}... xs={xs_url[:50]}... bid={bid} zone={zone_id}")

            # Phase 3: Notify server that video started
            logger.info("Notifying server: video started")
            await self.http_client.post_json(
                "/api/v1/videofeature/start",
                {"vrid": vrid},
                skip_reauth=True,
            )

            # Phase 4: Send progress ticks to fc.php (real 3s timing required)
            total_duration = 30
            tick_interval = 3
            atg = await self._get_atg_client()
            atg_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            }

            logger.info(f"Sending progress ticks ({total_duration}s with {tick_delay_ms}ms delays)...")
            for ts in range(0, total_duration + 1, tick_interval):
                remaining = total_duration - ts
                xc["ts"] = ts + tick_interval  # Mutate xc.ts BEFORE sending (matches JS behavior)

                try:
                    payload = {"self": xc, "at": ts, "rm": remaining, "b": str(bid), "z": str(zone_id)}
                    body = urlencode(_jquery_param(payload))
                    resp = await atg.post(fc_url, content=body.encode(), headers=atg_headers)
                    if resp.status_code == 200 and resp.text.strip():
                        try:
                            xc = resp.json()
                        except json.JSONDecodeError:
                            pass
                except Exception as e:
                    logger.warning(f"fc.php error at {ts}s: {e}")

                if ts < total_duration:
                    # Stealth: micro-jitter on tick timing (must stay close to 3s)
                    from ..stealth.timing import HumanTiming
                    tick_s = tick_delay_ms / 1000.0
                    await asyncio.sleep(max(2.0, HumanTiming.micro_jitter(tick_s, jitter_pct=0.1)))

            # Phase 5: Get signature from xs.php
            logger.info("Requesting signature from xs.php")
            try:
                xs_payload = {"self": xc, "csid": f"{bid}-{zone_id}", "val": 2}
                xs_body = urlencode(_jquery_param(xs_payload))
                xs_resp = await atg.post(xs_url, content=xs_body.encode(), headers=atg_headers)
                if xs_resp.status_code != 200:
                    return VideoRewardResult(
                        False, reward_type,
                        f"xs.php returned {xs_resp.status_code}: {xs_resp.text[:200]}"
                    )
                # Parse XML response for signature
                sign_match = re.search(r"<sign>(.*?)</sign>", xs_resp.text)
                signature = sign_match.group(1) if sign_match else ""
                if not signature:
                    return VideoRewardResult(False, reward_type, "Empty signature from xs.php (timing too fast?)")
                logger.info(f"Got signature: {signature}")
            except Exception as e:
                return VideoRewardResult(False, reward_type, f"xs.php failed: {e}")

            if not signature:
                return VideoRewardResult(False, reward_type, "Empty signature from xs.php")

            # Phase 6: Wait a bit then claim reward (human-like reaction)
            from ..stealth.timing import HumanTiming
            actual_wait = wait_before_claim_s + HumanTiming.reaction_time(base_ms=1500)
            logger.info(f"Waiting {actual_wait:.1f}s before claiming...")
            await asyncio.sleep(actual_wait)

            logger.info("Claiming reward via /videofeature/ends")
            ends_data = await self.http_client.post_json(
                "/api/v1/videofeature/ends",
                {"vrid": vrid, "hash": signature},
                skip_reauth=True,
            )

            if ends_data.get("error"):
                return VideoRewardResult(
                    False, reward_type,
                    f"Server rejected: {ends_data.get('error')} - {ends_data.get('message', '')}",
                    str(ends_data),
                )

            # For buildingUpgrade: follow the redirectTo URL to actually start the build
            redirect_to = ends_data.get("redirectTo")
            if redirect_to and reward_type == "buildingUpgrade":
                logger.info(f"Following buildingUpgrade redirect: {redirect_to}")
                await self.http_client.get_html(redirect_to, skip_reauth=True)

            logger.info(f"Reward claimed successfully! Type: {reward_type}")
            return VideoRewardResult(
                True, reward_type,
                f"Reward claimed: {REWARD_TYPES.get(reward_type, reward_type)}",
                str(ends_data),
            )

        except Exception as e:
            logger.error(f"Video reward failed: {e}")
            return VideoRewardResult(False, reward_type, f"Error: {e}")

    async def _extract_atg_config(self, iframe_url: str) -> Optional[Dict[str, Any]]:
        """Fetch iframe HTML and extract the base64-encoded ATG config."""
        try:
            full_url = iframe_url if iframe_url.startswith("http") else f"https:{iframe_url}"
            atg = await self._get_atg_client()
            resp = await atg.get(full_url)

            if resp.status_code != 200:
                logger.warning(f"Iframe fetch returned {resp.status_code}")
                return None

            html = resp.text

            # Extract base64 config — the ATG player wraps it in a custom atob
            # Pattern: n("eyJ...") where the base64 starts with eyJ (JSON opening)
            b64_match = re.search(r'"(eyJ[A-Za-z0-9+/=]{100,})"', html)
            if not b64_match:
                # Try the standard atob() pattern
                b64_match = re.search(r'atob\(["\']([^"\']+)["\']\)', html)
            
            if not b64_match:
                logger.warning("No base64 config found in iframe HTML")
                return None

            config_json = base64.b64decode(b64_match.group(1)).decode("utf-8")
            config = json.loads(config_json)
            
            # xsign can be at root or inside config
            if "xsign" not in config and "config" in config and "xsign" in config["config"]:
                config["xsign"] = config["config"]["xsign"]
            
            # zone_id can be in config subobject
            if "zone_id" not in config and "config" in config:
                config["zone_id"] = config["config"].get("zone_id")
            
            return config

        except Exception as e:
            logger.error(f"Failed to extract ATG config: {e}")
            return None

    async def get_available_rewards(self) -> Dict[str, bool]:
        """
        Check which video rewards are currently available.
        
        Returns:
            Dict mapping reward type to availability
        """
        try:
            resp = await self.http_client.post_json("/api/v1/graphql", {
                "query": """{ ownPlayer { productionBoost {
                    lumber { videoFeatureAvailable isActive }
                    clay { videoFeatureAvailable isActive }
                    iron { videoFeatureAvailable isActive }
                    crop { videoFeatureAvailable isActive }
                } } }"""
            })
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

