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
    
    # Check dorf1 for building activity
    html = await http.get_html('/dorf1.php')
    
    # Look for buildingList (queue display area)
    building_list = re.findall(r'buildingList.*?</ul>', html, re.DOTALL)
    for bl in building_list[:3]:
        print('BuildingList:', bl[:500])
    
    # Look for timer elements
    timer_contexts = []
    for m in re.finditer(r'timer\d+', html):
        start = max(0, m.start() - 200)
        end = min(len(html), m.end() + 200)
        timer_contexts.append(html[start:end])
    for tc in timer_contexts[:3]:
        print('\nTimer context:', tc[:400])
    
    # Look for ongoing construction / under construction
    ongoing = re.findall(r'(good_|underConstruction|isBeingUpgraded|upgradeInProgress|levelUp)[^"]*', html)
    print('\nOngoing markers:', ongoing[:20])
    
    # Now check dorf2 for building queue
    html2 = await http.get_html('/dorf2.php')
    building_list2 = re.findall(r'buildingList.*?</ul>', html2, re.DOTALL)
    for bl in building_list2[:3]:
        print('\nDorf2 BuildingList:', bl[:500])
    
    timer_contexts2 = []
    for m in re.finditer(r'timer\d+', html2):
        start = max(0, m.start() - 200)
        end = min(len(html2), m.end() + 200)
        timer_contexts2.append(html2[start:end])
    for tc in timer_contexts2[:3]:
        print('\nDorf2 Timer context:', tc[:400])
    
    # Check for Warehouse building
    wh = re.findall(r'[Ww]arehouse.*?level.*?\d+', html2)
    print('\nWarehouse refs:', wh[:5])
    
    await http.close()

asyncio.run(main())
