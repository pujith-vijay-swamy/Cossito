"""
browser_manager.py — Browser lifecycle management.

Creates and manages a single persistent ``browser-use`` BrowserSession
configured with anti-detection flags, a persistent user data directory,
and headed mode for human-in-the-loop interaction.

Compatible with browser-use v0.12.x where Browser/BrowserSession is a
flat Pydantic model (no separate BrowserConfig).
"""

from __future__ import annotations

import os
import asyncio
from urllib.parse import urlsplit
from pathlib import Path
from typing import Optional

from browser_use import Browser


# Default profile directory (project-local).
_DEFAULT_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "browser_profile"
)


class BrowserManager:
    """Singleton-style manager for the shared Browser instance.

    The same Browser (and therefore the same Playwright context, cookies,
    and DOM state) is used across the Recon and Execution phases so that
    no browser state is lost during the transition.
    """

    _active_instance: Optional[BrowserManager] = None

    def __init__(
        self,
        headless: bool = False,
        user_data_dir: Optional[str] = None,
    ) -> None:
        self._headless = headless
        print(f"[BrowserManager] Initialized with headless={self._headless}")
        self._user_data_dir = user_data_dir or _DEFAULT_PROFILE_DIR
        self._browser: Optional[Browser] = None
        self._lifecycle_lock = asyncio.Lock()

        # Ensure the profile directory exists.
        Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)

    def _build_browser(self) -> Browser:
        """Construct a Browser (BrowserSession) with anti-detection args and auto-granted media permissions."""
        print(f"[BrowserManager] Building browser with headless={self._headless}")
        return Browser(
            headless=self._headless,
            disable_security=False,
            enable_default_extensions=False,
            user_data_dir=self._user_data_dir,
            keep_alive=True,
            permissions=["microphone", "geolocation"],
            window_size={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--silent-debugger-extension-api",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-ui-for-media-stream",
                "--enable-usermedia-screen-capturing",
            ],
        )

    async def get_browser(self) -> Browser:
        """Return the shared Browser instance, creating it if necessary."""
        async with self._lifecycle_lock:
            return await self._get_browser_locked()

    async def _get_browser_locked(self) -> Browser:
        # Auto-close any previously active browser from an earlier agent run
        # to release the Playwright profile lock.
        if BrowserManager._active_instance and BrowserManager._active_instance != self:
            try:
                import asyncio
                # Set a strict 4-second timeout to prevent unresponsive close hangs from freezing the launch of the new browser!
                await asyncio.wait_for(BrowserManager._active_instance.close(), timeout=4.0)
            except Exception:
                # Force release the browser reference if close hung to unlock the thread
                if BrowserManager._active_instance:
                    BrowserManager._active_instance._browser = None
        BrowserManager._active_instance = self

        # Chrome owns profile locks. Never delete them while a session may be alive.

        if self._browser is None:
            self._browser = self._build_browser()
        return self._browser

    async def open_target_page(self, url: str, timeout: float = 45.0):
        """Navigate once and follow document commit/active-target replacement."""
        last_url = ''

        def identity(value):
            parts = urlsplit(value)
            return (parts.scheme.lower(), parts.hostname, parts.port or
                    (443 if parts.scheme == 'https' else 80),
                    parts.path or '/', parts.query, parts.fragment)

        async def open_page():
            nonlocal last_url
            browser = await self.get_browser()
            if not getattr(browser, 'is_cdp_connected', False):
                await browser.start()
            page = await browser.get_current_page()
            original_url = await page.get_url() if page is not None else ''
            if page is not None and identity(original_url) == identity(url):
                return page
            if page is None:
                await browser.navigate_to(url)
            else:
                await page.goto(url)

            # A navigation command completing is not a document commit. Both
            # branches can still expose blank/internal pages, or replace the
            # target object entirely. Never send another navigation while waiting.
            while True:
                active = await browser.get_current_page()
                candidates = [page] if page is not None else []
                if active is not None and active is not page:
                    candidates.append(active)
                for candidate in candidates:
                    try:
                        actual = await candidate.get_url()
                    except Exception:
                        # A detached old page is expected when the target changes.
                        continue
                    last_url = actual or ''
                    parts = urlsplit(last_url)
                    if parts.scheme in ('chrome-error', 'edge-error'):
                        raise RuntimeError('Navigation failed: the browser displayed a network error page')
                    if parts.scheme not in ('http', 'https') or not parts.hostname:
                        continue
                    if identity(actual) == identity(original_url):
                        continue
                    return candidate
                await asyncio.sleep(0.1)

        try:
            return await asyncio.wait_for(open_page(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            state = urlsplit(last_url).scheme or 'no active page'
            raise RuntimeError(f'Navigation failed: target document did not become available within {timeout:g}s (last state: {state})') from exc

    async def close(self) -> None:
        """Gracefully close the browser if it's open."""
        if self._browser is not None:
            try:
                import asyncio
                # Use a timeout of 5 seconds to prevent CDP socket close hangs from blocking the process
                await asyncio.wait_for(self._browser.close(), timeout=5.0)
            except Exception:
                pass  # Best-effort close.
            finally:
                self._browser = None

    @property
    def is_open(self) -> bool:
        return self._browser is not None
