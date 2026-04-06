import asyncio, sys, re
sys.path.insert(0, 'src')
from travian_api.config import Settings
from travian_api.clients.http_client import HttpClient
from travian_api.services.auth_service import AuthService

async def main():
    s = Settings()
    http = HttpClient(s)
    auth = AuthService(http, s)
    await auth.login()
    
    # Check dorf1 for under-construction fields
    html = await http.get_html('/dorf1.php')
    
    # Resource fields show "underConstruction" or "good" class with level info
    # Find all resource field area maps
    area_maps = re.findall(r'<area[^>]*href="/build\.php\?id=(\d+)[^"]*"[^>]*class="([^"]*)"[^>]*/>', html)
    for slot_id, cls in area_maps:
        if 'underConstruction' in cls or 'good' in cls:
            print(f'Slot {slot_id}: class={cls}')
    
    # Look for levelUp markers on specific slots
    levelups = re.findall(r'levelUp.*?slot(\d+)', html)
    print('\nLevelUp slots:', levelups)
    
    # Find the actual building queue display
    # Travian shows queue in a div with class "buildingList" or similar
    queue_html = re.findall(r'class="boxes buildingList".*?</div>\s*</div>', html, re.DOTALL)
    for qh in queue_html[:3]:
        print('\nQueue HTML:', qh[:500])
    
    # Also look for "Currently building" type text
    currently = re.findall(r'[Cc]urrently.*?(?:building|upgrading|construct)', html)
    print('\nCurrently building:', currently)
    
    # Check for specific slot states - look for "good" class with underConstruction
    field_slots = re.findall(r'<area[^>]*href="/build\.php\?id=(\d+)[^"]*"[^>]*class="[^"]*underConstruction[^"]*"', html)
    print('\nUnder construction slots:', field_slots)
    
    # Check the build page for slot 4 (Iron Mine that was at level 5)
    html4 = await http.get_html('/build.php?id=4')
    title4 = re.search(r'<h1[^>]*>(.*?)</h1>', html4)
    print(f'\nSlot 4 heading: {title4.group(1) if title4 else "none"}')
    
    # Check for any timer on this page
    timer4 = re.findall(r'timer.*?value="(\d+)"', html4)
    print(f'Slot 4 timers: {timer4}')
    
    # Check if slot 4 shows upgrade button or under construction
    if 'underConstruction' in html4:
        print('Slot 4 IS under construction!')
    
    # Check all slots quickly
    for slot in [1, 2, 3, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]:
        html_s = await http.get_html(f'/build.php?id={slot}')
        has_normal_upgrade = bool(re.search(r'section1.*?textButtonV1[^"]*green', html_s, re.DOTALL))
        has_gold_only = bool(re.search(r'section1.*?gold builder', html_s, re.DOTALL))
        is_under_construction = 'underConstruction' in html_s
        level_match = re.search(r'<span class="level">Level (\d+)</span>', html_s)
        lvl = level_match.group(1) if level_match else '?'
        status = 'BUILDING' if is_under_construction else ('green' if has_normal_upgrade else ('gold-only' if has_gold_only else 'unknown'))
        print(f'Slot {slot:2d} Lv{lvl}: {status}')
    
    await http.close()

asyncio.run(main())
