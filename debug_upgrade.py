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
    
    # Get fresh build page for slot 9
    html = await http.get_html('/build.php?id=9')
    
    # Look for upgrade links
    upgrade_links = re.findall(r'action=build[^"]*', html)
    print('Upgrade links:', upgrade_links)
    
    checksums = re.findall(r'checksum=(\w+)', html)
    print('Checksums:', checksums)
    
    if 'notNow' in html:
        print('NOT ENOUGH RESOURCES')
    if 'upgradeButtonsContainer' in html:
        print('Has upgrade buttons container')
    
    # Find the actual upgrade button area
    buttons = re.findall(r'button[^>]*build[^>]*>.*?</button>', html, re.DOTALL)
    for b in buttons[:5]:
        print('Button:', b[:200])
    
    # Find section1
    section1 = re.findall(r'section1.*?</div>', html, re.DOTALL)
    for s1 in section1[:2]:
        print('Section1:', s1[:300])
    
    # Now try the upgrade directly with correct checksum
    if checksums:
        cs = checksums[0]
        upgrade_url = f'/dorf1.php?id=9&gid=4&action=build&checksum={cs}'
        print(f'\nAttempting upgrade: {upgrade_url}')
        resp_html = await http.get_html(upgrade_url, skip_reauth=True)
        
        # Check what we got back
        title = re.search(r'<title>(.*?)</title>', resp_html)
        print(f'Response title: {title.group(1) if title else "none"}')
        print(f'Has showCancelBuildingDialog: {"showCancelBuildingDialog" in resp_html}')
        print(f'Has buildDuration: {"buildDuration" in resp_html}')
        print(f'Has underConstruction: {"underConstruction" in resp_html}')
        
        # Check for timer which indicates queue
        timers = re.findall(r'timer\d+', resp_html)
        print(f'Timers found: {timers}')
        
        # Extract queue from response
        queue_items = re.findall(r'buildingList.*?</ul>', resp_html, re.DOTALL)
        for qi in queue_items[:2]:
            print(f'Queue item: {qi[:300]}')
    
    # Now check queue
    from travian_api.services.building_service import BuildingService
    bs = BuildingService(http)
    queue = await bs.get_construction_queue()
    print(f'\nQueue items: {len(queue)}')
    for q in queue:
        print(f'  {q}')
    
    await http.close()

asyncio.run(main())
