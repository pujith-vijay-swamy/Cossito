"""Minimal test: can Agent + Browser actually open a page?"""
import asyncio
import os
from browser_use import Agent, Browser
from langchain_openai import ChatOpenAI

async def test():
    print("Creating Browser...")
    browser = Browser(
        headless=False,
        user_data_dir="./browser_profile",
        args=["--disable-blink-features=AutomationControlled"],
    )
    print(f"Browser type: {type(browser)}")

    # Check if we need to call start/connect first
    print("Creating a minimal LLM (will fail if no key, but browser should still open)...")
    
    # Test just starting the browser directly
    print("Calling browser.start()...")
    try:
        await browser.start()
        print("browser.start() succeeded!")
        page = await browser.get_current_page()
        print(f"Got page: {page}")
        await asyncio.sleep(5)
    except Exception as e:
        print(f"browser.start() failed: {e}")
    
    try:
        await browser.close()
    except:
        pass
    print("Done.")

asyncio.run(test())
