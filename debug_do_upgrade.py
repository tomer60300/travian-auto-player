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
    
    # Get checksum from slot 5 build page
    html5 = await http.get_html('/build.php?id=5')
    
    # Find the upgrade button onclick URL
    onclick = re.findall(r"window\.location\.href\s*=\s*'([^']*action=build[^']*)'", html5)
    print('Onclick URLs:')
    for url in onclick:
        clean = url.replace('&amp;', '&')
        print(f'  {clean}')
    
    # Use the first non-buildmaster URL
    upgrade_url = None
    for url in onclick:
        clean = url.replace('&amp;', '&')
        if 'buildmaster' not in clean:
            upgrade_url = clean
            break
    
    if not upgrade_url:
        # Try extracting from green build button
        cs = re.findall(r'checksum=([a-f0-9]+)', html5)
        if cs:
            upgrade_url = f'/dorf1.php?id=5&gid=2&action=build&checksum={cs[0]}'
    
    if upgrade_url:
        print(f'\nUpgrading with: {upgrade_url}')
        resp = await http.get_html(upgrade_url)
        
        # Check result
        title = re.search(r'<title>(.*?)</title>', resp)
        print(f'Title: {title.group(1) if title else "none"}')
        
        # Check if we got redirected to error or success
        if 'showCancelBuildingDialog' in resp:
            print('SUCCESS - Queue item created!')
        
        # Look for queue items on the response page
        timers = re.findall(r'class="timer"[^>]*value="(\d+)"[^>]*>(.*?)</span>', resp)
        print(f'Timers: {timers}')
        
        # Check buildingList
        bl = re.findall(r'buildingList.*?</ul>', resp, re.DOTALL)
        for b in bl[:2]:
            # Extract building names from list
            names = re.findall(r'<span class="name">(.*?)</span>', b)
            levels = re.findall(r'level (\d+)', b)
            print(f'Queue: names={names}, levels={levels}')
    else:
        print('No upgrade URL found!')
    
    await http.close()

asyncio.run(main())
