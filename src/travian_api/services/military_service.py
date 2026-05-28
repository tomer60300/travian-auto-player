"""Military service for Travian API — with stealth-aware delays."""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from ..clients.http_client import HttpClient
from ..concurrency import KeyedLock
from ..constants import EVENT_TYPES
from ..models.military import TroopSendResult
from ..parsers.html_parser import (
    clean_unicode,
    parse_rally_point_troops,
    parse_smithy_research_levels,
    parse_troop_confirm_page,
    parse_troop_overview,
)
from ..stealth.human_delay import ActionType
from .target_resolver import TargetResolver

logger = logging.getLogger(__name__)


class MilitaryService:
    """Service for military operations (scout, raid, attack)."""

    def __init__(self, http_client: HttpClient, target_resolver: TargetResolver):
        self.http_client = http_client
        self.target_resolver = target_resolver
        # Serializes scout/raid/attack dispatch per target tile so the same
        # coord isn't hit twice concurrently (would burn a second raid slot
        # and likely trip anti-bot heuristics).
        self._tile_lock = KeyedLock()

    async def send_scouts(
        self,
        x: int,
        y: int,
        amount: int,
        scout_type: str = "resources",
        village_id: Optional[int] = None,
    ) -> TroopSendResult:
        """
        Send scouts to target.

        Args:
            x, y: Target coordinates
            amount: Number of scouts
            scout_type: "resources" to scout resources, "defenses" to scout troops/defenses
            village_id: Source village ID (switches village if set)
        """
        # Teuton scout = t4, Gaul = t3, Roman = t4
        troops = {"t4": amount}

        # scoutTarget values: "1" = resources, "2" = defenses
        scout_target_value = "1" if scout_type == "resources" else "2"

        # eventType=4 (raid) — server auto-detects scout when only scout units present
        return await self._send_troops(
            x=x,
            y=y,
            troops=troops,
            event_type=EVENT_TYPES.get("raid", 4),
            scout_target=scout_target_value,
            village_id=village_id,
        )

    async def send_raid(
        self,
        x: int,
        y: int,
        troops: Dict[str, int],
        village_id: Optional[int] = None,
    ) -> TroopSendResult:
        """Send a raid (eventType=4)."""
        return await self._send_troops(
            x=x,
            y=y,
            troops=troops,
            event_type=EVENT_TYPES.get("raid", 4),
            village_id=village_id,
        )

    async def send_attack(
        self,
        x: int,
        y: int,
        troops: Dict[str, int],
        village_id: Optional[int] = None,
    ) -> TroopSendResult:
        """Send a normal attack (eventType=3)."""
        return await self._send_troops(
            x=x,
            y=y,
            troops=troops,
            event_type=EVENT_TYPES.get("attack", 3),
            village_id=village_id,
        )

    async def get_available_troops(self, village_id: Optional[int] = None) -> Dict[str, int]:
        """Get available troops at rally point."""
        url = "/build.php?gid=16&tt=2"
        if village_id:
            url = f"/build.php?newdid={village_id}&gid=16&tt=2"
        html = await self.http_client.get_html(url)
        return parse_rally_point_troops(html)

    async def get_smithy_research_levels(
        self,
        smithy_slot: int,
        village_id: Optional[int] = None,
        tribe_id: int = 0,
    ) -> Dict[str, int]:
        """Fetch the smithy build page and return per-unit research levels.

        Returns {t1..t10: level}. Slots that aren't visible on the page
        (e.g. unit not researchable for this tribe) stay at 0.
        """
        url = f"/build.php?id={smithy_slot}&gid=13"
        if village_id:
            url = f"/build.php?newdid={village_id}&id={smithy_slot}&gid=13"
        html = await self.http_client.get_html(url)
        return parse_smithy_research_levels(html, tribe_id=tribe_id)

    async def get_village_troop_totals(
        self,
        village_id: Optional[int] = None,
        tribe_id: int = 0,
    ) -> Dict[str, int]:
        """Get total troops for a village (in-village + outgoing + incoming).

        Fetches ``/village/statistics/troops`` which lists all troop categories.
        """
        url = "/village/statistics/troops"
        if village_id:
            url = f"/village/statistics/troops?newdid={village_id}"
        html = await self.http_client.get_html(url)
        return parse_troop_overview(html, tribe_id=tribe_id)

    # ── internal ─────────────────────────────────────────────────────

    async def _send_troops(
        self,
        x: int,
        y: int,
        troops: Dict[str, int],
        event_type: int,
        scout_target: str = "",
        village_id: Optional[int] = None,
    ) -> TroopSendResult:
        """
        Two-step troop sending with stealth:
        1. Navigate to rally point (if stealth enabled)
        2. POST form to /build.php?gid=16&tt=2 -> confirmation page
        3. Human delay (reading confirmation)
        4. Parse hidden fields + checksum, POST confirmation -> troops dispatched
        """
        async with self._tile_lock((x, y)):
            return await self._send_troops_unlocked(
                x, y, troops, event_type, scout_target, village_id
            )

    async def _send_troops_unlocked(
        self,
        x: int,
        y: int,
        troops: Dict[str, int],
        event_type: int,
        scout_target: str,
        village_id: Optional[int],
    ) -> TroopSendResult:
        try:
            # Stealth: navigate to rally point first
            await self.http_client.navigator.navigate_to_rally_point(village_id)

            # Use newdid in the POST URL to set village context
            if village_id:
                rally_url = f"/build.php?newdid={village_id}&gid=16&tt=2"
            else:
                rally_url = "/build.php?gid=16&tt=2"

            # ── Step 1: Submit troop selection form ──
            form_data = {}
            for i in range(1, 11):
                form_data[f"troop[t{i}]"] = str(troops.get(f"t{i}", 0))
            form_data["villagename"] = ""
            form_data["x"] = str(x)
            form_data["y"] = str(y)
            form_data["eventType"] = str(event_type)
            form_data["ok"] = "ok"

            # Stealth: human-like delay before submitting troop form
            delay = self.http_client.human_delay
            await delay.wait(ActionType.FORM_FILL, "filling troop selection form")

            logger.info(
                f"Step 1: Sending troop form to ({x},{y}) type={event_type} troops={troops}"
            )
            confirm_html = await self.http_client.post_form(
                rally_url, form_data, safe_to_retry=False
            )

            # Check for error — but only if we DON'T have a confirmation form
            has_confirm = "troopSendForm" in confirm_html or "confirmSendTroops" in confirm_html
            if not has_confirm:
                error_msg = self._extract_error(confirm_html)
                if error_msg and error_msg != "Unknown error":
                    return TroopSendResult(
                        success=False,
                        target_x=x,
                        target_y=y,
                        raw_response=f"Step 1 error: {error_msg}",
                    )
                # Also check for "No troops" text
                if "No troops" in confirm_html or "no troops" in confirm_html.lower():
                    return TroopSendResult(
                        success=False,
                        target_x=x,
                        target_y=y,
                        raw_response="Step 1 error: No troops have been selected.",
                    )
                # No confirmation form and no recognized error — the server
                # returned a generic page (rate-limit, session issue, etc.).
                # Do NOT fall through to Step 2; bail out immediately.
                logger.warning(
                    f"Step 1: No confirmation form for ({x},{y}), HTML length={len(confirm_html)}"
                )
                return TroopSendResult(
                    success=False,
                    target_x=x,
                    target_y=y,
                    raw_response=f"Step 1 error: No confirmation form returned (server returned {len(confirm_html)} bytes — possible rate limit or session issue)",
                )

            # ── Step 2: Parse confirmation page (with human delay for reading) ──
            await self.http_client.human_delay.wait(ActionType.RAPID, "reading troop confirmation")

            confirm_fields = parse_troop_confirm_page(confirm_html)
            checksum = confirm_fields.pop("checksum", "")

            if not checksum:
                # Try harder — look in button onclick
                cs_match = re.search(
                    r"input\[name=checksum\]'\)\.value\s*=\s*'([a-f0-9]+)'", confirm_html
                )
                if cs_match:
                    checksum = cs_match.group(1)

            if not checksum:
                return TroopSendResult(
                    success=False,
                    target_x=x,
                    target_y=y,
                    raw_response="No checksum found in confirmation page",
                )

            # Build final POST data from hidden fields
            final_data = {}
            for key, value in confirm_fields.items():
                final_data[key] = value

            # Add checksum
            final_data["checksum"] = checksum

            # Set scout target when sending only scouts
            # Values: "" = default (defense), "1" = resources, "2" = defenses
            if scout_target:
                final_data["troops[0][scoutTarget]"] = scout_target

            # Stealth: human reads the confirmation page before clicking send
            await delay.wait(ActionType.PAGE_LOAD, "reading troop confirmation")

            logger.info(f"Step 2: Confirming with checksum={checksum}")
            result_html = await self.http_client.post_form(
                rally_url, final_data, safe_to_retry=False
            )

            # Success detection: after confirming, the server returns the rally point
            # page. The key negative indicator is the confirmation form reappearing
            # with the SAME action token (means it wasn't processed).
            # Positive indicators: troop movements, rally overview, or simply
            # a valid page without the form reappearing or an error message.
            action_token = final_data.get("action", "")
            form_reappeared = action_token and f'value="{action_token}"' in result_html
            has_error = bool(re.search(r'class="error[^"]*"', result_html))
            has_troop_movement = "troopMovement" in result_html

            # Success: the old action token was consumed (not reappearing),
            # no error divs, and ideally we see troop movements on the page.
            # Note: 'confirmSendTroops' always appears as a button on the
            # rally point page — it does NOT indicate we're stuck confirming.
            success = not form_reappeared and not has_error
            if has_troop_movement:
                success = True

            # Try to extract travel time from the confirmation we parsed
            travel_time = ""
            time_match = re.search(r'class="in"[^>]*>.*?(\d+:\d+:\d+)', confirm_html, re.DOTALL)
            if time_match:
                travel_time = time_match.group(1)

            # Extract target name from confirmation
            target_name = ""
            vname_match = re.search(r'name="villagename"[^>]*value="([^"]*)"', confirm_html)
            if vname_match:
                target_name = clean_unicode(vname_match.group(1))

            return TroopSendResult(
                success=success,
                target_x=x,
                target_y=y,
                target_name=target_name,
                troops_sent=troops,
                travel_time=travel_time,
                raw_response=result_html[:500] if not success else "",
            )

        except Exception as e:
            logger.error(f"Troop send failed: {e}")
            return TroopSendResult(
                success=False,
                target_x=x,
                target_y=y,
                raw_response=str(e),
            )

    def _extract_error(self, html: str) -> str:
        """Extract error message from HTML response."""
        # Check class="error" divs
        match = re.search(r'class="error[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
        if match:
            return clean_unicode(re.sub(r"<[^>]+>", "", match.group(1)).strip())
        # Check for common Travian error patterns outside error divs
        for pattern in [
            r"beginner.{0,5}s?\s*protection",
            r"not enough troops",
            r"no troops",
            r"cannot (send|raid|attack)",
            r"too many troops",
            r"target.*not.*valid",
            r"rate.?limit",
        ]:
            err_match = re.search(pattern, html, re.IGNORECASE)
            if err_match:
                # Extract surrounding text for context
                start = max(0, err_match.start() - 20)
                end = min(len(html), err_match.end() + 80)
                snippet = re.sub(r"<[^>]+>", "", html[start:end]).strip()
                return clean_unicode(snippet)
        return "Unknown error"
