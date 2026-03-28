"""Report parsers for Travian battle and scout reports."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Any
from bs4 import BeautifulSoup
from datetime import datetime

from ..models.reports import Report, ReportListItem, BattleReportData, ScoutReportData
from .html_parser import clean_unicode


def parse_report_list(html: str) -> List[ReportListItem]:
    """
    Parse report list page (/report/all?page=N).

    Row structure:
      <input name="ids[]" value="{id}"/>
      <img class="iReport iReport{N}"/>   <- icon type
      <div><a href="?id=...">subject</a></div>
      <td class="dat">date</td>
    """
    soup = BeautifulSoup(html, 'html.parser')
    reports = []

    for checkbox in soup.find_all('input', {'name': 'ids[]'}):
        report_id = checkbox.get('value', '')
        if not report_id:
            continue

        row = checkbox.find_parent('tr')
        if not row:
            continue

        icon_type = 0
        report_type = "unknown"
        icon_img = row.find('img', class_=re.compile(r'iReport'))
        if icon_img:
            classes = ' '.join(icon_img.get('class', []))
            type_match = re.search(r'iReport(\d+)', classes)
            if type_match:
                icon_type = int(type_match.group(1))
                if 1 <= icon_type <= 8:
                    report_type = "battle"
                elif 11 <= icon_type <= 14:
                    report_type = "trade"
                elif 15 <= icon_type <= 19:
                    report_type = "scout"
                elif icon_type == 20:
                    report_type = "reinforcement"
                elif icon_type == 21:
                    report_type = "adventure"
                elif icon_type == 22:
                    report_type = "settlement"
                else:
                    report_type = "misc"

        subject = ""
        sub_cell = row.find('td', class_='sub')
        if sub_cell:
            div = sub_cell.find('div')
            if div:
                link = div.find('a')
                if link:
                    subject = clean_unicode(link.get_text(strip=True))

        date_str = ""
        dat_cell = row.find('td', class_='dat')
        if dat_cell:
            date_str = clean_unicode(dat_cell.get_text(strip=True))

        is_read = True
        status_img = row.find('img', class_=re.compile(r'messageStatus'))
        if status_img:
            status_classes = ' '.join(status_img.get('class', []))
            is_read = 'messageStatusUnread' not in status_classes

        reports.append(ReportListItem(
            report_id=report_id,
            icon_type=icon_type,
            report_type=report_type,
            subject=subject,
            date_str=date_str,
            is_read=is_read,
        ))

    return reports


def _parse_resource_wrapper(wrapper) -> Dict[str, int]:
    """
    Parse a resourceWrapper div.

    Actual structure:
      <div class="inlineIcon resources" title="Lumber">
        <i class="lumber"></i>
        <span class="value">1091</span>
      </div>
    """
    result = {}
    for icon_div in wrapper.find_all('div', class_='inlineIcon'):
        title = (icon_div.get('title') or '').lower()
        value_span = icon_div.find('span', class_='value')
        if not value_span:
            continue
        raw = clean_unicode(value_span.get_text(strip=True))
        # Strip any level text e.g. "Residence level 4" — skip non-numeric
        num_match = re.search(r'^(\d[\d,\.]*)', raw.replace(',', ''))
        if not num_match:
            continue
        val = int(num_match.group(1))
        # Map title to resource key
        if 'lumber' in title or 'wood' in title:
            result['lumber'] = val
        elif 'clay' in title:
            result['clay'] = val
        elif 'iron' in title:
            result['iron'] = val
        elif 'crop' in title:
            result['crop'] = val
    return result


def _parse_troops_table(table) -> Dict[str, int]:
    """
    Parse a troop table.

    Structure:
      <tbody class="units">
        <tr>
          <th class="coords"/>
          <td class="uniticon"><img class="unit u11" alt="Clubswinger"/></td>
          ...
        </tr>
      </tbody>
      <tbody class="units">
        <tr>
          <th>...</th>
          <td class="unit">5</td>   <- counts
          ...
        </tr>
      </tbody>
    """
    troops = {}
    tbodies = table.find_all('tbody', class_='units')
    if len(tbodies) < 2:
        return troops

    # First tbody: unit icons (headers)
    header_tbody = tbodies[0]
    unit_keys = []
    for td in header_tbody.find_all('td', class_='uniticon'):
        img = td.find('img', class_=re.compile(r'\bunit\b'))
        if img:
            alt = img.get('alt', '').strip()
            cls_list = img.get('class', [])
            uid = next((c for c in cls_list if re.match(r'^u\d+$|^uhero$', c)), None)
            unit_keys.append(uid or alt)
        else:
            unit_keys.append(None)

    # Second tbody: unit counts
    count_tbody = tbodies[1]
    count_row = count_tbody.find('tr')
    if not count_row:
        return troops

    count_cells = count_row.find_all('td', class_='unit')
    for key, cell in zip(unit_keys, count_cells):
        if key is None:
            continue
        raw = cell.get_text(strip=True).replace(',', '')
        if raw.isdigit():
            troops[key] = int(raw)

    return troops


def _parse_troops_losses(table) -> Dict[str, int]:
    """
    Parse troop losses from a troop table.

    Losses are in the third tbody (class 'units last') or a tbody with class 'losses'.
    Loss values are typically negative or shown as casualties.
    """
    losses = {}
    tbodies = table.find_all('tbody', class_='units')
    if len(tbodies) < 2:
        return losses

    # Unit keys from first tbody
    header_tbody = tbodies[0]
    unit_keys = []
    for td in header_tbody.find_all('td', class_='uniticon'):
        img = td.find('img', class_=re.compile(r'\bunit\b'))
        if img:
            cls_list = img.get('class', [])
            uid = next((c for c in cls_list if re.match(r'^u\d+$|^uhero$', c)), None)
            alt = img.get('alt', '').strip()
            unit_keys.append(uid or alt)
        else:
            unit_keys.append(None)

    # Third tbody (index 2) or tbody with 'last' class = losses
    loss_tbody = None
    if len(tbodies) >= 3:
        loss_tbody = tbodies[2]
    else:
        loss_tbody = table.find('tbody', class_='last')

    if not loss_tbody:
        return losses

    loss_row = loss_tbody.find('tr')
    if not loss_row:
        return losses

    loss_cells = loss_row.find_all('td', class_='unit')
    for key, cell in zip(unit_keys, loss_cells):
        if key is None:
            continue
        raw = cell.get_text(strip=True).replace(',', '')
        # Losses may appear as negative numbers or plain numbers
        cleaned = raw.lstrip('-')
        if cleaned.isdigit() and int(cleaned) > 0:
            losses[key] = int(cleaned)

    return losses


def _extract_player_village(soup: BeautifulSoup, role: str) -> Dict[str, Any]:
    """Extract player + village info from a role div (attacker/defender)."""
    info: Dict[str, Any] = {
        'player_name': '',
        'village_name': '',
        'village_id': 0,
        'coordinates': {'x': 0, 'y': 0},
    }

    role_div = soup.find('div', class_=re.compile(rf'\brole\s+{role}\b'))
    if not role_div:
        return info

    # Village link: <a href="/karte.php?d=ID">Village Name</a>
    # Note: some links are /karte.php with no text (map icon), skip those
    for a in role_div.find_all('a', href=re.compile(r'karte\.php')):
        href = a.get('href', '')
        name = clean_unicode(a.get_text(strip=True))
        if not name:
            continue
        did_match = re.search(r'd=(\d+)', href)
        if did_match:
            info['village_id'] = int(did_match.group(1))
        info['village_name'] = name
        break

    # Player link: <a href="/spieler.php?...">Name</a>
    for a in role_div.find_all('a', href=re.compile(r'spieler\.php')):
        name = clean_unicode(a.get_text(strip=True))
        if name:
            info['player_name'] = name
            break

    # Coordinates: look for coord text like "(−161|167)" or "(x|y)" in role div
    role_text = role_div.get_text(' ', strip=True)
    coord_match = re.search(r'\((-?\d+)\|(-?\d+)\)', role_text)
    if coord_match:
        info['coordinates'] = {'x': int(coord_match.group(1)), 'y': int(coord_match.group(2))}

    return info


def parse_scout_report(html: str) -> ScoutReportData:
    """
    Parse a scout report page.

    Extracts:
    - Resources at target (lumber/clay/iron/crop)
    - Cranny (hidden) amount + raidable amount
    - Defender troops (if visible)
    - Target village info
    """
    soup = BeautifulSoup(html, 'html.parser')

    target_info = _extract_player_village(soup, 'defender')

    # Resources: first resourceWrapper inside additionalInformation
    resources: Dict[str, int] = {'lumber': 0, 'clay': 0, 'iron': 0, 'crop': 0}
    cranny: int = 0
    raidable: int = 0

    ai_table = soup.find('table', class_='additionalInformation')
    if ai_table:
        wrappers = ai_table.find_all('div', class_='resourceWrapper')
        if wrappers:
            # First wrapper = actual resources
            resources.update(_parse_resource_wrapper(wrappers[0]))
        if len(wrappers) > 1:
            # Second wrapper = cranny + raidable
            for icon_div in wrappers[1].find_all('div', class_='inlineIcon'):
                title = (icon_div.get('title') or '').lower()
                value_span = icon_div.find('span', class_='value')
                if not value_span:
                    continue
                raw = clean_unicode(value_span.get_text(strip=True)).replace(',', '')
                if raw.isdigit():
                    val = int(raw)
                    if 'cranny' in title:
                        cranny = val
                    elif 'raidable' in title or 'carry' in title:
                        raidable = val

    # Defender troops (if visible)
    troops: Dict[str, int] = {}
    defender_role = soup.find('div', class_=re.compile(r'\brole\s+defender\b'))
    if defender_role:
        for table in defender_role.find_all('table'):
            t = _parse_troops_table(table)
            if t:
                troops.update(t)

    # Buildings from additionalInformation
    buildings: List[Dict[str, Any]] = []
    if ai_table:
        for row in ai_table.find_all('tr'):
            th = row.find('th')
            if th and 'building' in th.get_text(strip=True).lower():
                for icon_div in row.find_all('div', class_='inlineIcon'):
                    title = icon_div.get('title', '')
                    value_span = icon_div.find('span', class_='value')
                    if title and value_span:
                        buildings.append({'name': title, 'detail': value_span.get_text(strip=True)})

    return ScoutReportData(
        target=target_info,
        resources=resources,
        stealable_resources={'raidable': raidable, 'cranny': cranny},
        troops=troops,
        buildings=buildings,
    )


def parse_battle_report(html: str) -> BattleReportData:
    """Parse a battle report page."""
    soup = BeautifulSoup(html, 'html.parser')

    attacker_info = _extract_player_village(soup, 'attacker')
    defender_info = _extract_player_village(soup, 'defender')

    # Troops and losses per role
    attacker_troops: Dict[str, int] = {}
    defender_troops: Dict[str, int] = {}
    attacker_losses: Dict[str, int] = {}
    defender_losses: Dict[str, int] = {}

    attacker_role = soup.find('div', class_=re.compile(r'\brole\s+attacker\b'))
    if attacker_role:
        for table in attacker_role.find_all('table'):
            t = _parse_troops_table(table)
            if t:
                attacker_troops.update(t)
            l = _parse_troops_losses(table)
            if l:
                attacker_losses.update(l)

    defender_role = soup.find('div', class_=re.compile(r'\brole\s+defender\b'))
    if defender_role:
        for table in defender_role.find_all('table'):
            t = _parse_troops_table(table)
            if t:
                defender_troops.update(t)
            l = _parse_troops_losses(table)
            if l:
                defender_losses.update(l)

    # Battle result — from div.outcome elements
    # e.g. "Attacker with no losses" = attacker won
    # "Defender with no losses" = defender won
    # Both have losses = draw or partial
    battle_result = 'unknown'
    outcomes = [div.get_text(' ', strip=True).lower()
                for div in soup.find_all('div', class_='outcome')]
    if outcomes:
        attacker_outcome = next((o for o in outcomes if 'attacker' in o), '')
        defender_outcome = next((o for o in outcomes if 'defender' in o), '')
        atk_no_loss = 'no loss' in attacker_outcome
        def_no_loss = 'no loss' in defender_outcome
        if atk_no_loss and not def_no_loss:
            battle_result = 'victory'
        elif def_no_loss and not atk_no_loss:
            battle_result = 'defeat'
        elif atk_no_loss and def_no_loss:
            # Both no losses: attacker won if defender had no actual troops (all zeros = empty)
            has_def_troops = any(v > 0 for v in defender_troops.values())
            if not has_def_troops:
                battle_result = 'victory'
            else:
                battle_result = 'draw'
        else:
            battle_result = 'victory'  # both took losses but attacker was aggressor
    else:
        # Fallback: check string text
        for el in soup.find_all(string=re.compile(r'(victory|defeat|draw)', re.I)):
            t = el.strip().lower()
            if 'victory' in t:
                battle_result = 'victory'
                break
            elif 'defeat' in t:
                battle_result = 'defeat'
                break
            elif 'draw' in t:
                battle_result = 'draw'
                break

    # Bounty (resources stolen) — in resourceWrapper after battle
    bounty: Dict[str, int] = {'lumber': 0, 'clay': 0, 'iron': 0, 'crop': 0}
    ai_table = soup.find('table', class_='additionalInformation')
    if ai_table:
        rw = ai_table.find('div', class_='resourceWrapper')
        if rw:
            bounty.update(_parse_resource_wrapper(rw))

    return BattleReportData(
        attacker=attacker_info,
        defender=defender_info,
        attacker_troops=attacker_troops,
        defender_troops=defender_troops,
        battle_result=battle_result,
        bounty=bounty,
        attacker_losses=attacker_losses,
        defender_losses=defender_losses,
    )


# Scout unit IDs as they appear in HTML class attributes (e.g. class="unit u4")
# u4 = Equites Legati (Romans), u8 = Scout (Teutons), u12 = Pathfinder (Gauls)
SCOUT_UNIT_IDS = {'u4', 'u8', 'u12'}


def _has_troop_losses(soup: BeautifulSoup) -> bool:
    """Check if the report shows troop losses (indicates a battle, not a scout)."""
    # Loss rows in troop tables have class 'losses' or contain struck-through numbers
    for tbody in soup.find_all('tbody', class_='units last'):
        for td in tbody.find_all('td', class_='unit'):
            text = td.get_text(strip=True).replace(',', '')
            if text.lstrip('-').isdigit() and int(text) != 0:
                return True
    # Also check for casualty rows explicitly
    for span in soup.find_all('span', class_='casualty'):
        return True
    return False


def _get_attacker_unit_ids(soup: BeautifulSoup) -> set:
    """Extract set of unit IDs (e.g. {'u4'}) from the attacker's troop table."""
    unit_ids = set()
    attacker_role = soup.find('div', class_=re.compile(r'\brole\s+attacker\b'))
    if not attacker_role:
        return unit_ids
    for table in attacker_role.find_all('table'):
        troops = _parse_troops_table(table)
        for uid, count in troops.items():
            if count > 0:
                unit_ids.add(uid)
    return unit_ids


def parse_individual_report(html: str) -> Dict[str, Any]:
    """
    Detect report type and parse accordingly.

    Detection strategy:
    - Scout reports: additionalInformation with Resources row AND attacker troops
      are exclusively scout units (u4/u8/u12) or empty
    - Battle reports: have attacker/defender roles with non-scout troops or losses
    - Battle reports with bounty: additionalInformation has a resourceWrapper but
      attacker used combat troops — bounty is parsed as stolen resources

    Returns dict with keys: type, data
    """
    soup = BeautifulSoup(html, 'html.parser')

    has_attacker = bool(soup.find('div', class_=re.compile(r'\brole\s+attacker\b')))
    has_defender = bool(soup.find('div', class_=re.compile(r'\brole\s+defender\b')))

    # Check for additionalInformation table with Resources header
    ai_table = soup.find('table', class_='additionalInformation')
    has_resources_row = False
    if ai_table:
        for th in ai_table.find_all('th'):
            header_text = th.get_text(strip=True).lower()
            if header_text == 'resources':
                has_resources_row = True
                break

    if has_resources_row and (has_attacker or has_defender):
        # Both scout reports and battle-with-bounty have resources in additionalInformation.
        # Distinguish by checking what troops the attacker sent.
        attacker_units = _get_attacker_unit_ids(soup)
        is_scout_only = (not attacker_units) or attacker_units.issubset(SCOUT_UNIT_IDS)
        has_losses = _has_troop_losses(soup)

        if is_scout_only and not has_losses:
            return {'type': 'scout', 'data': parse_scout_report(html)}
        else:
            # Battle with bounty — parse as battle (bounty captured in parse_battle_report)
            return {'type': 'battle', 'data': parse_battle_report(html)}

    if has_attacker or has_defender:
        return {'type': 'battle', 'data': parse_battle_report(html)}

    return {
        'type': 'unknown',
        'data': {'html_snippet': html[:500]},
    }
