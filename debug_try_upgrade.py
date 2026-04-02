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
    
    # Check slot 5 (Clay Pit, marked "green")
    html5 = await http.get_html('/build.php?id=5')
    
    # Find all buttons/links with build action
    links = re.findall(r'href="([^"]*action=build[^"]*)"', html5)
    print('Slot 5 build links:')
    for l in links:
        print(f'  {l}')
    
    # Find button classes
    buttons = re.findall(r'<button[^>]*class="([^"]*)"[^>]*>(.*?)</button>', html5, re.DOTALL)
    for cls, text in buttons:
        if 'build' in cls.lower() or 'green' in cls or 'gold' in cls:
            print(f'Button: class={cls}, text={text[:100]}')
    
    # Check section1 for actual upgrade button
    s1_match = re.search(r'id="section1"(.*?)(?=id="section|$)', html5, re.DOTALL)
    if s1_match:
        s1 = s1_match.group(1)
        print(f'\nSection1 content (500 chars): {s1[:500]}')
    
    # Check slot 4 timer details
    html4 = await http.get_html('/build.php?id=4')
    timers4 = re.findall(r'class="timer"[^>]*value="(\d+)"[^>]*>(.*?)</span>', html4)
    print(f'\nSlot 4 timers: {timers4}')
    
    # Find what is being built on slot 4
    heading4 = re.search(r'<h1[^>]*class="[^"]*"[^>]*>(.*?)</h1>', html4, re.DOTALL)
    if heading4:
        print(f'Slot 4 heading: {heading4.group(1).strip()[:200]}')
    
    await http.close()

asyncio.run(main())
