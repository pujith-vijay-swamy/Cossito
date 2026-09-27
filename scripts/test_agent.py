import asyncio
from browser_use import Agent, Browser
from llm_provider import get_llm
import os
from langchain_core.globals import set_debug
set_debug(True)

async def test():
    # Use the default model from the app
    model_name = "openai/gpt-4o-mini"
    # Provide a dummy API key for testing if there isn't one
    # Note: openrouter/owl-alpha might need a real API key. 
    # But wait, openrouter API keys start with sk-or-v1-
    # I will just print the error that comes back.
    api_key = os.environ.get("OPENROUTER_API_KEY", "sk-or-v1-dummy-key")
    
    print(f"Creating LLM with model={model_name}")
    try:
        llm = get_llm(model=model_name, api_key=api_key)
    except Exception as e:
        print(f"Failed to create LLM: {e}")
        return

    print("Creating Browser...")
    browser = Browser(headless=False, user_data_dir="./browser_profile")
    
    print("Creating Agent...")
    agent = Agent(
        task="Go to https://example.com and tell me the title.",
        llm=llm,
        browser=browser
    )
    
    print("Running Agent...")
    try:
        history = await agent.run(max_steps=2)
        print("Agent finished. Result:")
        print(history.final_result())
    except Exception as e:
        print(f"Agent run failed: {e}")
        import traceback
        traceback.print_exc()
        
    await browser.close()
    print("Done")

asyncio.run(test())
