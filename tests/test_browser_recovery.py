import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from browser_manager import BrowserManager

class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, browser):
        mgr=BrowserManager.__new__(BrowserManager)
        mgr._browser=browser
        mgr._closing=False
        mgr._recovery_lock=asyncio.Lock()
        return mgr

    async def test_concurrent_callers_share_reconnect(self):
        browser=SimpleNamespace(is_cdp_connected=False)
        async def reconnect():
            await asyncio.sleep(.01)
            browser.is_cdp_connected=True
        browser.reconnect=AsyncMock(side_effect=reconnect)
        mgr=self.manager(browser)
        await asyncio.gather(mgr.ensure_connection(),mgr.ensure_connection())
        browser.reconnect.assert_awaited_once()

    async def test_waits_for_library_recovery_without_starting_another(self):
        event=asyncio.Event()
        browser=SimpleNamespace(is_cdp_connected=False,_reconnecting=True,_reconnect_event=event,reconnect=AsyncMock())
        mgr=self.manager(browser)
        async def complete():
            await asyncio.sleep(.01)
            browser.is_cdp_connected=True
            browser._reconnecting=False
            event.set()
        await asyncio.gather(mgr.ensure_connection(),complete())
        browser.reconnect.assert_not_awaited()

    async def test_connected_client_replacement_reacquires_exact_tab(self):
        wanted=SimpleNamespace(_target_id='lesson')
        other=SimpleNamespace(_target_id='other')
        browser=SimpleNamespace(is_cdp_connected=True,get_pages=AsyncMock(return_value=[other,wanted]),reconnect=AsyncMock())
        mgr=self.manager(browser)
        self.assertIs(await mgr.recover_page(SimpleNamespace(_target_id='lesson')),wanted)
        browser.reconnect.assert_not_awaited()

    async def test_missing_target_does_not_use_another_tab(self):
        browser=SimpleNamespace(is_cdp_connected=True,get_pages=AsyncMock(return_value=[SimpleNamespace(_target_id='other')]))
        with self.assertRaisesRegex(ConnectionError,'no longer open'):
            await self.manager(browser).recover_page(SimpleNamespace(_target_id='lesson'))

    async def test_close_never_reconnects(self):
        browser=SimpleNamespace(is_cdp_connected=False,reconnect=AsyncMock())
        mgr=self.manager(browser)
        mgr._closing=True
        with self.assertRaises(ConnectionError):
            await mgr.ensure_connection()
        browser.reconnect.assert_not_awaited()
