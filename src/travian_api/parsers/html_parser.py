"""HTML parsers for Travian game pages."""

from __future__ import annotations

import re
import json
from typing import Dict, List, Optional, Any
from bs4 import BeautifulSoup

from ..constants import BUILDING_NAMES, RESOURCE_NAMES
from ..models.buildings import Resources, BuildingDetail, QueueItem


def clean_unicode(text: str) -> str:
    """
    Clean Unicode directional markers from text.
    
    Args:
        text: Text that may contain U+202D and U+202C markers
        
    Returns:
        Cleaned text
    """
    # Remove Unicode directional override markers
    text = text.replace('\u202d', '').replace('\u202c', '')
    return text.strip()


def parse_dorf1(html: str) -> List[Dict[str, Any]]:
    """
    Parse dorf1.php (resource fields) HTML.
    
    Args:
        html: Raw HTML content from dorf1.php
        
    Returns:
        List of resource field dictionaries
    """
    soup = BeautifulSoup(html, 'html.parser')
    fields = []
    
    # Find resource field links (slots 1-18)
    for slot in range(1, 19):
        # Look for links with buildingSlot{N} class
        link = soup.find('a', class_=lambda x: x and f'buildingSlot{slot}' in x)
        
        if not link:
            continue
            
        # Extract building info from classes
        classes = ' '.join(link.get('class', []))
        
        # Extract gid and aid from data attributes
        gid = link.get('data-gid')
        aid = link.get('data-aid')
        
        # Extract level from classes (levelN)
        level_match = re.search(r'level(\d+)', classes)
        level = int(level_match.group(1)) if level_match else 0
        
        # Determine building type from gid
        building_name = BUILDING_NAMES.get(int(gid) if gid else 0, f'Unknown (gid={gid})')
        
        fields.append({
            'slot_id': slot,
            'aid': int(aid) if aid else slot,
            'gid': int(gid) if gid else 0,
            'name': building_name,
            'level': level,
            'classes': classes
        })
    
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
    soup = BeautifulSoup(html, 'html.parser')
    buildings = []
    
    # Find all build.php links
    for link in soup.find_all('a', href=re.compile(r'/build\.php\?.*id=\d+')):
        href = link.get('href', '')
        classes = ' '.join(link.get('class', []))
        
        # Extract slot ID from href
        id_match = re.search(r'[?&]id=(\d+)', href)
        if not id_match:
            continue
        slot_id = int(id_match.group(1))
        
        # Skip resource field slots (1-18) and rally point link with tt= param
        if slot_id < 19 or 'tt=' in href:
            continue
        
        if 'emptyBuildingSlot' in classes:
            buildings.append({
                'slot_id': slot_id,
                'gid': 0,
                'name': 'Empty',
                'level': 0,
                'classes': classes
            })
            continue
        
        # Extract gid from data-gid attr, class (gidN), or href
        gid = 0
        data_gid = link.get('data-gid')
        if data_gid:
            gid = int(data_gid)
        else:
            gid_match = re.search(r'\bgid(\d+)\b', classes)
            if gid_match:
                gid = int(gid_match.group(1))
            else:
                # Try href param
                gid_href_match = re.search(r'[?&]gid=(\d+)', href)
                if gid_href_match:
                    gid = int(gid_href_match.group(1))
        
        # Extract level from title (e.g. "Main Building Level 2||...")
        level = 0
        title = link.get('title', '')
        level_match = re.search(r'Level (\d+)', title)
        if level_match:
            level = int(level_match.group(1))
        else:
            # Try from class
            level_match = re.search(r'\blevel(\d+)\b', classes)
            if level_match:
                level = int(level_match.group(1))
            else:
                # Try label layer div
                label = link.find('div', class_='labelLayer')
                if label:
                    try:
                        level = int(label.get_text(strip=True))
                    except ValueError:
                        pass
        
        # If we still don't have gid, try to reverse-lookup from building name in title
        building_name = ""
        if gid:
            building_name = BUILDING_NAMES.get(gid, f'Unknown (gid={gid})')
        elif title:
            # Title format: "Main Building Level 2||..."
            title_name = title.split('Level')[0].split('||')[0].strip()
            if title_name:
                building_name = title_name
                # Reverse lookup gid from name
                name_to_gid = {v: k for k, v in BUILDING_NAMES.items()}
                gid = name_to_gid.get(title_name, 0)
        
        if not building_name:
            building_name = f'Unknown (gid={gid})'
        
        buildings.append({
            'slot_id': slot_id,
            'gid': gid,
            'name': building_name,
            'level': level,
            'classes': classes,
            'href': href
        })
    
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
    js_match = re.search(r'var\s+resources\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
    
    if not js_match:
        return Resources()
    
    try:
        # Clean up JavaScript object to make it JSON-parsable
        js_obj = js_match.group(1)
        # Quote unquoted keys
        js_obj = re.sub(r'([{\s,])(\w+)\s*:', r'\1"\2":', js_obj)
        
        resource_data = json.loads(js_obj)
        
        production = resource_data.get('production', {})
        storage = resource_data.get('storage', {})
        max_storage = resource_data.get('maxStorage', {})
        
        return Resources(
            lumber=int(storage.get('l1', 0)),
            clay=int(storage.get('l2', 0)),
            iron=int(storage.get('l3', 0)),
            crop=int(storage.get('l4', 0)),
            free_crop=int(production.get('l5', 0)),
            max_lumber=int(max_storage.get('l1', 0)),
            max_clay=int(max_storage.get('l2', 0)),
            max_iron=int(max_storage.get('l3', 0)),
            max_crop=int(max_storage.get('l4', 0)),
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
    soup = BeautifulSoup(html, 'html.parser')
    
    # Try to extract slot ID from HTML if not provided
    if not slot_id:
        slot_match = re.search(r'build\.php\?id=(\d+)', html)
        if slot_match:
            slot_id = int(slot_match.group(1))
    
    # Extract building name and level from titleInHeader:
    #   <h1 class="titleInHeader">Woodcutter <span class="level">Level 5</span></h1>
    name = ""
    level = 0
    title_header = soup.find(class_='titleInHeader')
    if title_header:
        # Level is in <span class="level">
        level_span = title_header.find('span', class_='level')
        if level_span:
            level_match = re.search(r'(\d+)', level_span.get_text())
            if level_match:
                level = int(level_match.group(1))
        # Name is the text before the span
        full_text = title_header.get_text(separator=' ').strip()
        name = re.sub(r'\s*Level\s+\d+.*', '', full_text).strip()
    
    if not name:
        # Fallback to any Level text
        level_text = soup.find(string=re.compile(r'Level \d+'))
        if level_text:
            lm = re.search(r'Level (\d+)', level_text)
            if lm:
                level = int(lm.group(1))
    
    # Extract gid — prefer from upgrade URL (most specific), then reverse-lookup from name
    gid = 0
    # Defer gid extraction until after we find the upgrade URL (below)
    # It will be extracted from the URL like /dorf1.php?id=6&gid=2&action=build&checksum=...
    
    # If we got a name but no gid, reverse-lookup from BUILDING_NAMES
    if name and not gid:
        name_to_gid = {v.lower(): k for k, v in BUILDING_NAMES.items()}
        gid = name_to_gid.get(name.lower(), 0)
    
    # If we have gid but no name, look it up
    if gid and not name:
        name = BUILDING_NAMES.get(gid, f'Unknown (gid={gid})')
    elif not name:
        name = f'Unknown (gid={gid})'
    
    # Extract checksum from upgrade button onclick
    checksum = ""
    upgrade_url = ""
    gold_upgrade_url = ""
    
    # Look for buttons with checksum in onclick — prefer non-gold (no buildmaster)
    for button in soup.find_all(['button', 'a'], onclick=True):
        onclick = button.get('onclick', '')
        checksum_match = re.search(r'checksum=([a-f0-9]{6})', onclick)
        if checksum_match:
            found_checksum = checksum_match.group(1)
            
            # Extract full URL
            url_match = re.search(r"window\.location\.href = '([^']+)'", onclick)
            found_url = ""
            if url_match:
                found_url = url_match.group(1).replace('&amp;', '&')
            
            if 'buildmaster' not in found_url:
                # This is the free upgrade button — preferred
                checksum = found_checksum
                upgrade_url = found_url
                break
            else:
                # Gold button — store as fallback, keep looking for free one
                if not checksum:
                    checksum = found_checksum
                    gold_upgrade_url = found_url
    
    # If we only found the gold button, construct the free URL from it
    if not upgrade_url and gold_upgrade_url:
        upgrade_url = gold_upgrade_url.replace('&buildmaster', '')
    
    # Extract gid from the upgrade URL (most reliable source)
    if upgrade_url:
        gid_from_url = re.search(r'gid=(\d+)', upgrade_url)
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
    contract = soup.find(id='contract')
    if contract:
        resource_map = {'r1': 'lumber', 'r2': 'clay', 'r3': 'iron', 'r4': 'crop'}
        for icon in contract.find_all('i', class_=re.compile(r'r[1-4]')):
            icon_class = ' '.join(icon.get('class', []))
            for prefix, res_name in resource_map.items():
                if prefix in icon_class:
                    # Value is in the next sibling span
                    value_span = icon.find_next('span', class_='value')
                    if value_span:
                        val_text = clean_unicode(value_span.get_text(strip=True))
                        val_match = re.search(r'[\d,]+', val_text)
                        if val_match:
                            costs[res_name] = int(val_match.group().replace(',', ''))
                    break
    
    # Extract construction time
    construction_time = ""
    time_element = soup.find(text=re.compile(r'\d+:\d+:\d+'))
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
    soup = BeautifulSoup(html, 'html.parser')
    buildings = []

    for wrapper in soup.find_all('div', class_='buildingWrapper'):
        wrapper_id = wrapper.get('id', '')
        gid_match = re.search(r'contract_building(\d+)', wrapper_id)
        if not gid_match:
            continue
        gid = int(gid_match.group(1))

        # Building name from h2
        h2 = wrapper.find('h2')
        name = clean_unicode(h2.get_text(strip=True)) if h2 else BUILDING_NAMES.get(gid, f'Unknown (gid={gid})')

        # Construction URL from button onclick
        build_url = ""
        checksum = ""
        btn = wrapper.find(['button', 'a'], onclick=re.compile(r'action=build'))
        if btn:
            url_match = re.search(r"window\.location\.href = '([^']+)'", btn.get('onclick', ''))
            if url_match:
                build_url = url_match.group(1).replace('&amp;', '&')
            cs_match = re.search(r'checksum=([a-f0-9]+)', btn.get('onclick', ''))
            if cs_match:
                checksum = cs_match.group(1)

        # Costs from #contract div within this wrapper
        costs = {}
        contract = wrapper.find(id='contract')
        target = contract if contract else wrapper
        resource_map = {'r1': 'lumber', 'r2': 'clay', 'r3': 'iron', 'r4': 'crop'}
        for icon in target.find_all('i', class_=re.compile(r'r[1-4]')):
            icon_class = ' '.join(icon.get('class', []))
            for prefix, res_name in resource_map.items():
                if prefix in icon_class:
                    value_span = icon.find_next('span', class_='value')
                    if value_span:
                        val_text = clean_unicode(value_span.get_text(strip=True))
                        val_match = re.search(r'[\d,]+', val_text)
                        if val_match:
                            costs[res_name] = int(val_match.group().replace(',', ''))
                    break

        buildings.append({
            'gid': gid,
            'name': name,
            'checksum': checksum,
            'build_url': build_url,
            'costs': costs,
            'can_build': bool(build_url),
        })

    return buildings


def parse_construction_queue(html: str) -> List[QueueItem]:
    """
    Parse construction queue from buildingList div.
    
    Args:
        html: Raw HTML content containing buildingList
        
    Returns:
        List of queue item dicts
    """
    soup = BeautifulSoup(html, 'html.parser')
    queue_items = []
    
    # Find buildingList container (class, not id — Travian uses class="buildingList")
    building_list = soup.find('div', class_='buildingList')
    if not building_list:
        return queue_items

    # Look for construction items — <li> tags inside buildingList (may have no class)
    for item in building_list.find_all('li'):
        # Extract event ID from cancel dialog onclick
        event_id = ""
        cancel_link = item.find('a', onclick=re.compile(r'showCancelBuildingDialog'))
        if cancel_link:
            onclick = cancel_link.get('onclick', '')
            event_match = re.search(r'showCancelBuildingDialog\(\s*(\d+)', onclick)
            if event_match:
                event_id = event_match.group(1)

        if not event_id:
            continue  # Not a construction item

        # Extract building name — try dedicated span first, fallback to first text node
        building_name = "Unknown"
        name_span = item.find('span', class_='buildingName')
        if name_span:
            building_name = name_span.get_text(strip=True)
        else:
            # Fallback: first meaningful text in the <li>
            item_text = item.get_text(strip=True)
            name_match = re.match(r'^([A-Za-z ]+?)(?:Level|Lv)', item_text)
            if name_match:
                building_name = name_match.group(1).strip()

        # Extract target level
        target_level = 0
        level_match = re.search(r'(?:to )?[Ll]evel\s*(\d+)', item.get_text())
        if level_match:
            target_level = int(level_match.group(1))

        # Extract remaining time from timer span (class="timer" with value= attribute)
        remaining_seconds = 0
        timer_span = item.find('span', class_='timer', counting='down')
        if not timer_span:
            # Fallback: any timer span with a value attribute
            timer_span = item.find('span', class_='timer')
        if timer_span:
            # Prefer the 'value' attribute (seconds) — most reliable
            value_attr = timer_span.get('value', '')
            if value_attr and value_attr.isdigit():
                remaining_seconds = int(value_attr)
            else:
                # Fallback: parse text format (H:MM:SS)
                timer_text = timer_span.get_text(strip=True)
                time_parts = timer_text.split(':')
                if len(time_parts) == 3:
                    try:
                        hours = int(time_parts[0])
                        minutes = int(time_parts[1])
                        seconds = int(time_parts[2])
                        remaining_seconds = hours * 3600 + minutes * 60 + seconds
                    except ValueError:
                        pass

        queue_items.append(QueueItem(
            event_id=event_id,
            building_name=building_name,
            target_level=target_level,
            remaining_seconds=remaining_seconds,
        ))
    
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
    for troop_num in range(1, 11):
        key = f't{troop_num}'
        troops[key] = 0
        
        # Pattern: troop[tN] input followed by an <a> with .val(NUMBER) in onclick
        pattern = rf"troop\[{key}\].*?\.val\((\d+)\)"
        match = re.search(pattern, html, re.DOTALL)
        if match:
            troops[key] = int(match.group(1))
    
    return troops


def parse_troop_confirm_page(html: str) -> Dict[str, Any]:
    """
    Parse troop sending confirmation page for hidden fields.
    
    Args:
        html: Raw HTML content from troop confirmation page
        
    Returns:
        Dictionary with hidden form fields and checksum
    """
    soup = BeautifulSoup(html, 'html.parser')
    fields = {}
    
    # Extract all hidden inputs
    for hidden_input in soup.find_all('input', type='hidden'):
        name = hidden_input.get('name', '')
        value = hidden_input.get('value', '')
        
        if name:
            fields[name] = value
    
    # Extract checksum from confirm button onclick
    checksum = ""
    confirm_button = soup.find('button', onclick=re.compile(r'checksum='))
    if confirm_button:
        onclick = confirm_button.get('onclick', '')
        checksum_match = re.search(r'checksum=([a-f0-9]{6})', onclick)
        if checksum_match:
            checksum = checksum_match.group(1)
    
    fields['checksum'] = checksum
    
    # Also extract visible fields like coordinates and village name
    coord_inputs = soup.find_all('input', {'name': re.compile(r'[xy]')})
    for coord_input in coord_inputs:
        name = coord_input.get('name', '')
        value = coord_input.get('value', '')
        if name in ['x', 'y']:
            fields[name] = value
    
    # Extract village name if present
    village_input = soup.find('input', {'name': 'villagename'})
    if village_input:
        fields['villagename'] = village_input.get('value', '')
    
    return fields