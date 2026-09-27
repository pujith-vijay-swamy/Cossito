import asyncio
from browser_use import Browser

async def test():
    b = Browser(headless=False, user_data_dir="./browser_profile")
    await b.start()
    page = await b.get_current_page()
    await page.goto('https://github.com/login')
    
    url = await b.get_current_page_url()
    print("CURRENT URL IS:", url)
    
    await b.close()

asyncio.run(test())
