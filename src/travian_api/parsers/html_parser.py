"""HTML parsers for Travian game pages."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from ..constants import BUILDING_GID_BY_NAME, BUILDING_GID_BY_NAME_LOWER, BUILDING_NAMES
from ..models.buildings import BuildingDetail, QueueItem, Resources


def clean_unicode(text: str) -> str:
    """
    Clean Unicode directional markers from text.

    Args:
        text: Text that may contain U+202D and U+202C markers

    Returns:
        Cleaned text
    """
    # Remove Unicode directional override markers
    text = text.replace("\u202d", "").replace("\u202c", "")
    return text.strip()


def parse_dorf1(html: str) -> List[Dict[str, Any]]:
    """
    Parse dorf1.php (resource fields) HTML.

    Args:
        html: Raw HTML content from dorf1.php

    Returns:
        List of resource field dictionaries
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = []

    # Find resource field links (slots 1-18)
    for slot in range(1, 19):
        # Look for links with buildingSlot{N} class
        link = soup.find("a", class_=lambda x: x and f"buildingSlot{slot}" in x)

        if not link:
            continue

        # Extract building info from classes
        classes = " ".join(link.get("class", []))

        # Extract gid and aid from data attributes
        gid = link.get("data-gid")
        aid = link.get("data-aid")

        # Extract level from classes (levelN)
        level_match = re.search(r"level(\d+)", classes)
        level = int(level_match.group(1)) if level_match else 0

        # Determine building type from gid
        building_name = BUILDING_NAMES.get(int(gid) if gid else 0, f"Unknown (gid={gid})")

        fields.append(
            {
                "slot_id": slot,
                "aid": int(aid) if aid else slot,
                "gid": int(gid) if gid else 0,
                "name": building_name,
                "level": level,
                "classes": classes,
            }
        )

    return fields


def parse_dorf2(html: str) -> List[Dict[str, Any]]:
    """
    Parse dorf2.php (village buildings) HTML.

    Real HTML uses links like:
      <a href="/build.php?id=26" class="level colorLayer notNow aid26 teuton" ... title="Main Building Level 2||...">
      <a href="/build.php?id=19" class="emptyBuildingSlot">

    Args:
        html: Raw HTML content from dorf2.php

    Returns:
        List of building dictionaries
    """
    soup = BeautifulSoup(html, "html.parser")
    buildings = []

    # Find all build.php links
    for link in soup.find_all("a", href=re.compile(r"/build\.php\?.*id=\d+")):
        href = link.get("href", "")
        classes = " ".join(link.get("class", []))

        # Extract slot ID from href
        id_match = re.search(r"[?&]id=(\d+)", href)
        if not id_match:
            continue
        slot_id = int(id_match.group(1))

        # Skip resource field slots (1-18) and rally point link with tt= param
        if slot_id < 19 or "tt=" in href:
            continue

        if "emptyBuildingSlot" in classes:
            buildings.append(
                {"slot_id": slot_id, "gid": 0, "name": "Empty", "level": 0, "classes": classes}
            )
            continue

        # Extract gid from data-gid attr, class (gidN), or href
        gid = 0
        data_gid = link.get("data-gid")
        if data_gid:
            gid = int(data_gid)
        else:
            gid_match = re.search(r"\bgid(\d+)\b", classes)
            if gid_match:
                gid = int(gid_match.group(1))
            else:
                # Try href param
                gid_href_match = re.search(r"[?&]gid=(\d+)", href)
                if gid_href_match:
                    gid = int(gid_href_match.group(1))

        # Extract level from title (e.g. "Main Building Level 2||...")
        level = 0
        title = link.get("title", "")
        level_match = re.search(r"Level (\d+)", title)
        if level_match:
            level = int(level_match.group(1))
        else:
            # Try from class
            level_match = re.search(r"\blevel(\d+)\b", classes)
            if level_match:
                level = int(level_match.group(1))
            else:
                # Try label layer div
                label = link.find("div", class_="labelLayer")
                if label:
                    try:
                        level = int(label.get_text(strip=True))
                    except ValueError:
                        pass

        # If we still don't have gid, try to reverse-lookup from building name in title
        building_name = ""
        if gid:
            building_name = BUILDING_NAMES.get(gid, f"Unknown (gid={gid})")
        elif title:
            # Title format: "Main Building Level 2||..."
            title_name = title.split("Level")[0].split("||")[0].strip()
            if title_name:
                building_name = title_name
                # Reverse lookup gid from name
                gid = BUILDING_GID_BY_NAME.get(title_name, 0)

        if not building_name:
            building_name = f"Unknown (gid={gid})"

        buildings.append(
            {
                "slot_id": slot_id,
                "gid": gid,
                "name": building_name,
                "level": level,
                "classes": classes,
                "href": href,
            }
        )

    # Also parse buildingSlot divs that have data-aid/data-gid/data-name but
    # whose <a> child has no href (e.g. the wall slot).
    parsed_slots = {b["slot_id"] for b in buildings}
    for div in soup.find_all("div", class_="buildingSlot"):
        data_aid = div.get("data-aid")
        data_gid = div.get("data-gid")
        data_name = div.get("data-name")
        if not (data_aid and data_gid and data_name):
            continue
        slot_id = int(data_aid)
        if slot_id < 19 or slot_id in parsed_slots:
            continue
        parsed_slots.add(slot_id)
        gid = int(data_gid)
        level = 0
        child_a = div.find("a", attrs={"data-level": True})
        if child_a:
            try:
                level = int(child_a["data-level"])
            except (ValueError, KeyError):
                pass
        if level == 0:
            label = div.find("div", class_="labelLayer")
            if label:
                try:
                    level = int(label.get_text(strip=True))
                except ValueError:
                    pass
        building_name = data_name if data_name else BUILDING_NAMES.get(gid, f"Unknown (gid={gid})")
        buildings.append(
            {
                "slot_id": slot_id,
                "gid": gid,
                "name": building_name,
                "level": level,
                "classes": " ".join(div.get("class", [])),
                "href": "",
            }
        )

    return buildings


def parse_resources(html: str) -> Resources:
    """
    Parse resource information from JavaScript in HTML.

    Args:
        html: Raw HTML content containing resource JS

    Returns:
        Resources model
    """
    # Extract resources from JavaScript — the var spans multiple lines with nested objects
    js_match = re.search(r"var\s+resources\s*=\s*(\{.+?\})\s*;", html, re.DOTALL)

    if not js_match:
        return Resources()

    try:
        # Clean up JavaScript object to make it JSON-parsable
        js_obj = js_match.group(1)
        # Quote unquoted keys
        js_obj = re.sub(r"([{\s,])(\w+)\s*:", r'\1"\2":', js_obj)

        resource_data = json.loads(js_obj)

        production = resource_data.get("production", {})
        storage = resource_data.get("storage", {})
        max_storage = resource_data.get("maxStorage", {})

        return Resources(
            lumber=int(storage.get("l1", 0)),
            clay=int(storage.get("l2", 0)),
            iron=int(storage.get("l3", 0)),
            crop=int(storage.get("l4", 0)),
            free_crop=int(production.get("l5", 0)),
            max_lumber=int(max_storage.get("l1", 0)),
            max_clay=int(max_storage.get("l2", 0)),
            max_iron=int(max_storage.get("l3", 0)),
            max_crop=int(max_storage.get("l4", 0)),
            lumber_per_hour=int(production.get("l1", 0)),
            clay_per_hour=int(production.get("l2", 0)),
            iron_per_hour=int(production.get("l3", 0)),
            crop_per_hour=int(production.get("l4", 0)),
        )

    except (json.JSONDecodeError, KeyError, ValueError):
        return Resources()


def parse_build_page(html: str, slot_id: int = 0) -> BuildingDetail:
    """
    Parse build.php page for building details.

    Args:
        html: Raw HTML content from build.php
        slot_id: Building slot ID (passed from caller, since URL param is most reliable)

    Returns:
        BuildingDetail model
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try to extract slot ID from HTML if not provided
    if not slot_id:
        slot_match = re.search(r"build\.php\?id=(\d+)", html)
        if slot_match:
            slot_id = int(slot_match.group(1))

    # Extract building name and level from titleInHeader:
    #   <h1 class="titleInHeader">Woodcutter <span class="level">Level 5</span></h1>
    name = ""
    level = 0
    title_header = soup.find(class_="titleInHeader")
    if title_header:
        # Level is in <span class="level">
        level_span = title_header.find("span", class_="level")
        if level_span:
            level_match = re.search(r"(\d+)", level_span.get_text())
            if level_match:
                level = int(level_match.group(1))
        # Name is the text before the span
        full_text = title_header.get_text(separator=" ").strip()
        name = re.sub(r"\s*Level\s+\d+.*", "", full_text).strip()

    if not name:
        # Fallback to any Level text
        level_text = soup.find(string=re.compile(r"Level \d+"))
        if level_text:
            lm = re.search(r"Level (\d+)", level_text)
            if lm:
                level = int(lm.group(1))

    # Extract gid — prefer from upgrade URL (most specific), then reverse-lookup from name
    gid = 0
    # Defer gid extraction until after we find the upgrade URL (below)
    # It will be extracted from the URL like /dorf1.php?id=6&gid=2&action=build&checksum=...

    # If we got a name but no gid, reverse-lookup from BUILDING_NAMES
    if name and not gid:
        gid = BUILDING_GID_BY_NAME_LOWER.get(name.lower(), 0)

    # If we have gid but no name, look it up
    if gid and not name:
        name = BUILDING_NAMES.get(gid, f"Unknown (gid={gid})")
    elif not name:
        name = f"Unknown (gid={gid})"

    # Extract checksum from upgrade button onclick
    checksum = ""
    upgrade_url = ""
    gold_upgrade_url = ""

    # Look for buttons with checksum in onclick — prefer non-gold (no buildmaster)
    for button in soup.find_all(["button", "a"], onclick=True):
        onclick = button.get("onclick", "")
        checksum_match = re.search(r"checksum=([a-f0-9]{6})", onclick)
        if checksum_match:
            found_checksum = checksum_match.group(1)

            # Extract full URL
            url_match = re.search(r"window\.location\.href = '([^']+)'", onclick)
            found_url = ""
            if url_match:
                found_url = url_match.group(1).replace("&amp;", "&")

            if "buildmaster" not in found_url:
                # This is the free upgrade button — preferred
                checksum = found_checksum
                upgrade_url = found_url
                break
            # Gold button — store as fallback, keep looking for free one
            if not checksum:
                checksum = found_checksum
                gold_upgrade_url = found_url

    # If we only found the gold button, construct the free URL from it
    if not upgrade_url and gold_upgrade_url:
        upgrade_url = gold_upgrade_url.replace("&buildmaster", "")

    # Extract gid from the upgrade URL (most reliable source)
    if upgrade_url:
        gid_from_url = re.search(r"gid=(\d+)", upgrade_url)
        if gid_from_url:
            gid = int(gid_from_url.group(1))

    # Fallback: extract gid from the build.php URL in the page header area
    if not gid:
        gid_from_page = re.search(r'build\.php\?[^"]*gid=(\d+)', html[:5000])
        if gid_from_page:
            gid = int(gid_from_page.group(1))

    # Extract costs from #contract section
    # Real HTML: <i class="r1Big"></i><span class="value">115</span>
    costs = {}
    contract = soup.find(id="contract")
    if contract:
        resource_map = {"r1": "lumber", "r2": "clay", "r3": "iron", "r4": "crop"}
        for icon in contract.find_all("i", class_=re.compile(r"r[1-4]")):
            icon_class = " ".join(icon.get("class", []))
            for prefix, res_name in resource_map.items():
                if prefix in icon_class:
                    # Value is in the next sibling span
                    value_span = icon.find_next("span", class_="value")
                    if value_span:
                        val_text = clean_unicode(value_span.get_text(strip=True))
                        val_match = re.search(r"[\d,]+", val_text)
                        if val_match:
                            costs[res_name] = int(val_match.group().replace(",", ""))
                    break

    # Extract construction time
    construction_time = ""
    time_element = soup.find(text=re.compile(r"\d+:\d+:\d+"))
    if time_element:
        construction_time = time_element.strip()

    return BuildingDetail(
        slot_id=slot_id,
        gid=gid,
        name=name,
        level=level,
        costs=costs,
        construction_time=construction_time,
        checksum=checksum,
        upgrade_url=upgrade_url,
    )


def parse_empty_slot_buildings(html: str, slot_id: int = 0) -> List[Dict[str, Any]]:
    """
    Parse available buildings from an empty slot page (build.php?id={emptySlot}).

    Each building is in a div.buildingWrapper with id="contract_building{gid}",
    containing an h2 with the name, a #contract div with costs, and a button
    with onclick containing the construction URL.

    Args:
        html: Raw HTML content from build.php for an empty slot
        slot_id: Building slot ID

    Returns:
        List of dicts with keys: gid, name, checksum, build_url, costs
    """
    soup = BeautifulSoup(html, "html.parser")
    buildings = []

    for wrapper in soup.find_all("div", class_="buildingWrapper"):
        wrapper_id = wrapper.get("id", "")
        gid_match = re.search(r"contract_building(\d+)", wrapper_id)
        if not gid_match:
            continue
        gid = int(gid_match.group(1))

        # Building name from h2
        h2 = wrapper.find("h2")
        name = (
            clean_unicode(h2.get_text(strip=True))
            if h2
            else BUILDING_NAMES.get(gid, f"Unknown (gid={gid})")
        )

        # Construction URL from button onclick
        build_url = ""
        checksum = ""
        btn = wrapper.find(["button", "a"], onclick=re.compile(r"action=build"))
        if btn:
            url_match = re.search(r"window\.location\.href = '([^']+)'", btn.get("onclick", ""))
            if url_match:
                build_url = url_match.group(1).replace("&amp;", "&")
            cs_match = re.search(r"checksum=([a-f0-9]+)", btn.get("onclick", ""))
            if cs_match:
                checksum = cs_match.group(1)

        # Costs from #contract div within this wrapper
        costs = {}
        contract = wrapper.find(id="contract")
        target = contract if contract else wrapper
        resource_map = {"r1": "lumber", "r2": "clay", "r3": "iron", "r4": "crop"}
        for icon in target.find_all("i", class_=re.compile(r"r[1-4]")):
            icon_class = " ".join(icon.get("class", []))
            for prefix, res_name in resource_map.items():
                if prefix in icon_class:
                    value_span = icon.find_next("span", class_="value")
                    if value_span:
                        val_text = clean_unicode(value_span.get_text(strip=True))
                        val_match = re.search(r"[\d,]+", val_text)
                        if val_match:
                            costs[res_name] = int(val_match.group().replace(",", ""))
                    break

        buildings.append(
            {
                "gid": gid,
                "name": name,
                "checksum": checksum,
                "build_url": build_url,
                "costs": costs,
                "can_build": bool(build_url),
            }
        )

    return buildings


def parse_construction_queue(html: str) -> List[QueueItem]:
    """
    Parse construction queue from buildingList div.

    Args:
        html: Raw HTML content containing buildingList

    Returns:
        List of queue item dicts
    """
    soup = BeautifulSoup(html, "html.parser")
    queue_items = []

    # Find buildingList container (class, not id — Travian uses class="buildingList")
    building_list = soup.find("div", class_="buildingList")
    if not building_list:
        return queue_items

    # Look for construction items — <li> tags inside buildingList (may have no class)
    for item in building_list.find_all("li"):
        # Extract event ID from cancel dialog onclick
        event_id = ""
        cancel_link = item.find("a", onclick=re.compile(r"showCancelBuildingDialog"))
        if cancel_link:
            onclick = cancel_link.get("onclick", "")
            event_match = re.search(r"showCancelBuildingDialog\(\s*(\d+)", onclick)
            if event_match:
                event_id = event_match.group(1)

        if not event_id:
            continue  # Not a construction item

        # Extract building name — try dedicated span first, fallback to first text node
        building_name = "Unknown"
        name_span = item.find("span", class_="buildingName")
        if name_span:
            building_name = name_span.get_text(strip=True)
        else:
            # Fallback: first meaningful text in the <li>
            item_text = item.get_text(strip=True)
            name_match = re.match(r"^([A-Za-z ]+?)(?:Level|Lv)", item_text)
            if name_match:
                building_name = name_match.group(1).strip()

        # Extract target level
        target_level = 0
        level_match = re.search(r"(?:to )?[Ll]evel\s*(\d+)", item.get_text())
        if level_match:
            target_level = int(level_match.group(1))

        # Extract remaining time from timer span (class="timer" with value= attribute)
        remaining_seconds = 0
        timer_span = item.find("span", class_="timer", counting="down")
        if not timer_span:
            # Fallback: any timer span with a value attribute
            timer_span = item.find("span", class_="timer")
        if timer_span:
            # Prefer the 'value' attribute (seconds) — most reliable
            value_attr = timer_span.get("value", "")
            if value_attr and value_attr.isdigit():
                remaining_seconds = int(value_attr)
            else:
                # Fallback: parse text format (H:MM:SS)
                timer_text = timer_span.get_text(strip=True)
                time_parts = timer_text.split(":")
                if len(time_parts) == 3:
                    try:
                        hours = int(time_parts[0])
                        minutes = int(time_parts[1])
                        seconds = int(time_parts[2])
                        remaining_seconds = hours * 3600 + minutes * 60 + seconds
                    except ValueError:
                        pass

        queue_items.append(
            QueueItem(
                event_id=event_id,
                building_name=building_name,
                target_level=target_level,
                remaining_seconds=remaining_seconds,
            )
        )

    return queue_items


def parse_rally_point_troops(html: str) -> Dict[str, int]:
    """
    Parse available troops from rally point page.

    Real HTML structure per troop:
      <input name="troop[t1]" .../>&nbsp;/&nbsp;
      <a href="#" onclick="jQuery('...').val(428)...">&#x202d;428&#x202c;</a>

    The count is in the <a> onclick: .val(NUMBER)

    Args:
        html: Raw HTML content from rally point (build.php?gid=16&tt=2)

    Returns:
        Dictionary mapping troop types (t1-t10) to counts
    """
    troops = {}

    # Find troop counts from the onclick links: .val(N) patterns near troop[tN] inputs
    # Only count troops whose input field is NOT disabled (disabled = troops are out)
    for troop_num in range(1, 11):
        key = f"t{troop_num}"
        troops[key] = 0

        # First check if the input is disabled
        input_match = re.search(rf'name="troop\[{key}\]"[^>]*', html)
        if input_match and "disabled" in input_match.group(0):
            continue  # Skip disabled troop types — they can't be sent

        # Pattern: troop[tN] input followed by an <a> with .val(NUMBER) in onclick
        pattern = rf"troop\[{key}\].*?\.val\((\d+)\)"
        match = re.search(pattern, html, re.DOTALL)
        if match:
            troops[key] = int(match.group(1))

    return troops


# Class tokens that conventionally hide an element via a stylesheet rule
# (e.g. `.hidden{display:none}`). Best-effort: the real gid=17 markup is
# uncaptured, so we cannot resolve computed CSS — matching the common hiding
# classes catches the usual honeypot pattern without a full CSS engine.
_HIDDEN_CLASS_TOKENS = frozenset({"hidden", "hide", "hideme", "invisible", "d-none", "dn", "none"})


def _element_is_visible(el) -> bool:
    """Whether an element (and its chain) is visible to a real user.

    Honeypot guard: a route present in the markup but hidden — display:none,
    visibility:hidden, a `hidden` attribute, aria-hidden, or a conventional
    hiding class — is one a human could never see or act on, so acting on it
    (disabling it) is a pure bot signal. Checks the element and its ancestors.
    """
    node = el
    while node is not None and getattr(node, "attrs", None) is not None:
        if node.get("hidden") is not None:
            return False
        if str(node.get("aria-hidden", "")).lower() == "true":
            return False
        style = str(node.get("style", "")).replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return False
        classes = node.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        if any(c.lower() in _HIDDEN_CLASS_TOKENS for c in classes):
            return False
        node = node.parent
    return True


def parse_trade_routes(html: str) -> List[Dict[str, Any]]:
    """Existing trade routes from a marketplace (gid=17) page.

    Returns dicts ``{route_id, dest_x, dest_y, visible}``. BEST-EFFORT: the
    exact gid=17 markup has not been captured, so this recognizes the common
    Travian shape (a route row exposing a numeric route id plus destination
    coordinates) and returns an EMPTY list for markup it does not recognize —
    never a guess. An empty list means "nothing to disable", which is the safe
    default. Hidden rows are flagged ``visible=False`` so callers can ignore
    honeypot entries.
    """
    soup = BeautifulSoup(html, "html.parser")
    routes: List[Dict[str, Any]] = []
    seen: set[int] = set()

    for el in soup.find_all(attrs={"data-route-id": True}):
        try:
            route_id = int(el.get("data-route-id"))
        except (TypeError, ValueError):
            continue
        if route_id in seen:
            continue
        x = el.get("data-x")
        y = el.get("data-y")
        try:
            dest_x, dest_y = int(x), int(y)
        except (TypeError, ValueError):
            # Coordinates not on the same element — skip rather than guess.
            continue
        seen.add(route_id)
        routes.append(
            {
                "route_id": route_id,
                "dest_x": dest_x,
                "dest_y": dest_y,
                "visible": _element_is_visible(el),
            }
        )
    return routes


def parse_troop_confirm_page(html: str) -> Dict[str, Any]:
    """
    Parse troop sending confirmation page for hidden fields.

    Args:
        html: Raw HTML content from troop confirmation page

    Returns:
        Dictionary with hidden form fields and checksum
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = {}

    # Extract all hidden inputs
    for hidden_input in soup.find_all("input", type="hidden"):
        name = hidden_input.get("name", "")
        value = hidden_input.get("value", "")

        if name:
            fields[name] = value

    # Extract checksum from confirm button onclick
    checksum = ""
    confirm_button = soup.find("button", onclick=re.compile(r"checksum="))
    if confirm_button:
        onclick = confirm_button.get("onclick", "")
        checksum_match = re.search(r"checksum=([a-f0-9]{6})", onclick)
        if checksum_match:
            checksum = checksum_match.group(1)

    fields["checksum"] = checksum

    # Also extract visible fields like coordinates and village name
    coord_inputs = soup.find_all("input", {"name": re.compile(r"[xy]")})
    for coord_input in coord_inputs:
        name = coord_input.get("name", "")
        value = coord_input.get("value", "")
        if name in ["x", "y"]:
            fields[name] = value

    # Extract village name if present
    village_input = soup.find("input", {"name": "villagename"})
    if village_input:
        fields["villagename"] = village_input.get("value", "")

    return fields


# ---------------------------------------------------------------------------
# /village/statistics/* — account-wide tables (one row per village)
# ---------------------------------------------------------------------------

# Anchored on a query-parameter boundary so lookalikes (``oldnewdid=``) miss.
_STATS_NEWDID_RE = re.compile(r"[?&]newdid=(\d+)")

# Travian wraps every number in bidi override marks.
_STATS_BIDI = dict.fromkeys(
    [0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069],
    None,
)

# Digit grouping varies by locale: 1,750 / 1.750 / 1 750 / 1'750. These tables
# only ever carry integers, so every separator can go.
_STATS_GROUPING = str.maketrans("", "", ",. \t\xa0 '’")


def _stats_int(text: str) -> int:
    """Parse a statistics-table cell (``‭1,750‬``) into an int, 0 if unparseable.

    Returning 0 rather than raising keeps one odd cell from failing a whole
    export. Non-numeric content stays 0 on purpose: a ratio cell like ``19/20``
    must not collapse into 1920, and ``—`` genuinely means absent.
    """
    cleaned = text.translate(_STATS_BIDI).replace("−", "-").translate(_STATS_GROUPING).strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else 0


def _stats_village_rows(html: str, table_id: str) -> List[Any]:
    """Yield ``(village_id, data_cells)`` for each village row of a statistics table.

    Every village row carries a ``newdid=`` link in its first cell; the trailing
    ``Sum`` row and the blank spacer row do not, so requiring the link filters
    them out without depending on row classes. ``data_cells`` excludes the
    leading village cell.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=table_id)
    if table is None:
        return []

    rows: List[Any] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        link = tr.find("a", href=_STATS_NEWDID_RE)
        if link is None:
            continue
        match = _STATS_NEWDID_RE.search(link.get("href", ""))
        if match is None:
            continue
        # Slice after the cell that actually holds the village link rather than
        # assuming index 0, so a future leading cell cannot shift every column.
        cells = tr.find_all("td")
        village_cell = link.find_parent("td")
        start = next((i for i, cell in enumerate(cells) if cell is village_cell), None)
        if start is None:
            continue
        data_cells = cells[start + 1 :]
        if not data_cells:
            continue
        rows.append((int(match.group(1)), data_cells))
    return rows


def _stats_ratio(text: str) -> tuple[int, int]:
    """Parse a ``free/total`` cell such as ``‭‭19‬/‭20‬‬`` -> ``(19, 20)``.

    Returns ``(0, 0)`` when the cell is not a ratio. Note the *second* number is
    the village's merchant count; the first is how many are idle right now, so
    reading the wrong one understates capacity on a busy village.
    """
    parts = text.split("/")
    if len(parts) != 2:
        return (0, 0)
    return (_stats_int(parts[0]), _stats_int(parts[1]))


def parse_village_stats_resources(html: str) -> Dict[int, Dict[str, int]]:
    """Parse /village/statistics/resources per village.

    Returns ``{village_id: {lumber, clay, iron, crop, merchants_free,
    merchants_total}}``. ``merchants_total`` settles profile open question #2 --
    the count is read directly rather than derived from Marketplace level, and it
    genuinely varies (2 and 19 both observed alongside the usual 20).
    """
    out: Dict[int, Dict[str, int]] = {}
    # Travian spells this table id "ressources" — not a typo on our side.
    for vid, cells in _stats_village_rows(html, "ressources"):
        if len(cells) < 4:
            continue
        free, total = _stats_ratio(cells[4].get_text()) if len(cells) >= 5 else (0, 0)
        out[vid] = {
            "lumber": _stats_int(cells[0].get_text()),
            "clay": _stats_int(cells[1].get_text()),
            "iron": _stats_int(cells[2].get_text()),
            "crop": _stats_int(cells[3].get_text()),
            "merchants_free": free,
            "merchants_total": total,
        }
    return out


def parse_village_stats_production(html: str) -> Dict[int, Dict[str, int]]:
    """Parse /village/statistics/resources/production -> per-hour rates per village."""
    out: Dict[int, Dict[str, int]] = {}
    for vid, cells in _stats_village_rows(html, "production"):
        if len(cells) < 4:
            continue
        out[vid] = {
            "lumber": _stats_int(cells[0].get_text()),
            "clay": _stats_int(cells[1].get_text()),
            "iron": _stats_int(cells[2].get_text()),
            "crop": _stats_int(cells[3].get_text()),
        }
    return out


def parse_village_stats_capacity(html: str) -> Dict[int, Dict[str, int]]:
    """Parse /village/statistics/resources/capacity -> ``{village_id: {warehouse, granary}}``."""
    out: Dict[int, Dict[str, int]] = {}
    for vid, cells in _stats_village_rows(html, "capacity"):
        if len(cells) < 2:
            continue
        out[vid] = {
            "warehouse": _stats_int(cells[0].get_text()),
            "granary": _stats_int(cells[1].get_text()),
        }
    return out


def parse_village_stats_warehouse(html: str) -> Dict[int, Dict[str, Any]]:
    """Parse /village/statistics/resources/warehouse -> fill/empty countdowns.

    This page is the cheapest source of **true net crop** for the whole account:
    the countdown is the server's own calculation, so deriving a rate from it
    needs no upkeep model and no per-village fetch. See
    docs/20-resource-production.md.

    Returns ``{village_id: {crop_percent, crop_seconds, crop_draining,
    warehouse_seconds}}``. ``crop_draining`` is the direction of the granary
    countdown -- True counts down to empty (starving), False to full.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for vid, cells in _stats_village_rows(html, "warehouse"):
        # village | lumber% | clay% | iron% | warehouse timer | crop% | granary timer
        if len(cells) < 6:
            continue
        granary_cell = cells[5]
        granary_timer = granary_cell.find("span", class_="timer")
        if granary_timer is None:
            continue
        # Raw seconds live in the attributes; the rendered H:MM:SS is a JS
        # countdown and would have to be re-parsed for nothing.
        raw_seconds = granary_timer.get("data-value") or granary_timer.get("value") or ""
        warehouse_timer = cells[3].find("span", class_="timer")
        warehouse_raw = ""
        if warehouse_timer is not None:
            warehouse_raw = warehouse_timer.get("data-value") or warehouse_timer.get("value") or ""

        out[vid] = {
            "crop_percent": _stats_int(cells[4].get_text().replace("%", "")),
            "crop_seconds": _stats_int(str(raw_seconds)),
            # A draining granary prefixes a `crit`-classed minus span, so detect
            # it anywhere in the cell. The timer span itself is unreliable: the
            # live page emits it with a DUPLICATE class attribute
            # (`class="timer crit" class="timer"`) and BeautifulSoup keeps the
            # second, dropping `crit` — which read every village as filling.
            "crop_draining": granary_cell.find(class_="crit") is not None,
            "warehouse_seconds": _stats_int(str(warehouse_raw)),
        }
    return out


def parse_village_stats_troops(html: str, tribe_id: int = 0) -> Dict[int, Dict[str, int]]:
    """Parse /village/statistics/troops -> ``{village_id: {t1..t10}}`` for every village.

    Reads the account-wide grid: a header row of ``<img class="unit uNN">``
    icons defines the column order, and each body row is one village. Unit CSS
    ids are global, offset by
    tribe — Romans u1-u10, Teutons u11-u20, Gauls u21-u30 — so ``uNN`` maps back
    to ``tN`` by subtracting the tribe offset. The hero column (``uhero``) has no
    ``tN`` slot and is skipped.

    Only the player's own tribe ever appears here, so when ``tribe_id`` is
    unknown the column ids still identify the slots on their own — fall back to
    ``(uNN - 1) % 10 + 1`` rather than discarding every column and silently
    reporting an army of zero.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="troops")
    if table is None:
        return {}

    tribe_offset = max(0, (tribe_id - 1)) * 10 if tribe_id else 0

    column_keys: List[Any] = []
    head = table.find("thead")
    for img in head.find_all("img", class_="unit") if head else []:
        uid = next((c for c in img.get("class", []) if re.match(r"^u\d+$", c)), None)
        if uid is None:
            column_keys.append(None)
            continue
        unit_number = int(uid[1:])
        index = unit_number - tribe_offset if tribe_id else (unit_number - 1) % 10 + 1
        column_keys.append(f"t{index}" if 1 <= index <= 10 else None)

    out: Dict[int, Dict[str, int]] = {}
    for vid, cells in _stats_village_rows(html, "troops"):
        counts = {f"t{i}": 0 for i in range(1, 11)}
        for key, cell in zip(column_keys, cells):
            if key is not None:
                counts[key] = _stats_int(cell.get_text())
        out[vid] = counts
    return out


def parse_smithy_research_levels(html: str, tribe_id: int = 0) -> Dict[str, int]:
    """Parse smithy build page and return researched levels per troop slot.

    The smithy page (build.php?id=<smithy_slot>&gid=13) lists every troop the
    tribe can research. Each row follows roughly this layout:

      <div class="research" id="research11">
        <img class="unit u11" alt="Clubswinger" />
        <div class="information">
          <h4 class="title">
            <span>Clubswinger</span>
            <span class="level">Level 5</span>
          </h4>
        </div>
      </div>

    Unit CSS classes follow the same tribe-offset convention used elsewhere:
    Romans u1–u10, Teutons u11–u20, Gauls u21–u30.

    Returns a dict {t1..t10: level}. Missing rows stay at 0 (not researched).
    If ``tribe_id`` is 0, we infer the offset from the smallest u-ID seen on
    the page so we still work even when the caller hasn't passed tribe info.
    """
    soup = BeautifulSoup(html, "html.parser")
    levels: Dict[str, int] = {f"t{i}": 0 for i in range(1, 11)}

    # Each researchable troop sits inside a container that carries the unit
    # icon. We find the icon first, then look in the surrounding ancestor for
    # the "Level N" text. Walking up from the icon is the most version-stable
    # anchor — the heading/level markup has moved between T4.4 and T5+.
    icons = soup.find_all("img", class_=re.compile(r"\bunit\b"))
    if not icons:
        return levels

    # Infer tribe offset from the lowest u-ID on the page when not provided.
    inferred_offset = None
    u_ids: List[int] = []
    for icon in icons:
        for cls in icon.get("class", []):
            m = re.match(r"^u(\d+)$", cls)
            if m:
                u_ids.append(int(m.group(1)))
                break
    if u_ids:
        # Player tribe units occupy a contiguous block of 10 u-IDs starting at
        # 1+10*(tribe-1). Round the lowest down to that block boundary.
        inferred_offset = ((min(u_ids) - 1) // 10) * 10

    tribe_offset = max(0, (tribe_id - 1)) * 10 if tribe_id else (inferred_offset or 0)

    for icon in icons:
        uid_num = None
        for cls in icon.get("class", []):
            m = re.match(r"^u(\d+)$", cls)
            if m:
                uid_num = int(m.group(1))
                break
        if uid_num is None:
            continue
        relative = uid_num - tribe_offset
        if not (1 <= relative <= 10):
            continue

        # Walk up to the nearest <div class="research"> (or any ancestor that
        # contains the level text). Bounded to 5 hops so we don't sweep the
        # whole page on a stray icon (e.g. ongoing-research banner).
        container = icon
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            container = parent
            if container.name == "div" and "research" in (container.get("class") or []):
                break

        text = container.get_text(" ", strip=True) if container else ""
        # "Level 5" — case-insensitive, tolerant of "Lvl"/"Lv." abbreviations.
        m = re.search(r"\b(?:level|lvl|lv\.?)\s+(\d+)", text, re.IGNORECASE)
        if m:
            levels[f"t{relative}"] = int(m.group(1))

    return levels
