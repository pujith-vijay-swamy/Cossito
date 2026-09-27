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
from pathlib import Path
from typing import Optional

from browser_use import Browser


# Default profile directory (project-local).
_DEFAULT_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "browser_profile"
)


def _cleanup_profile_locks(user_data_dir: str) -> None:
    """Remove stale Chrome lockfiles that prevent CDP debugging server attachment."""
    p = Path(user_data_dir)
    lock_files = ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"]
    for lf in lock_files:
        try:
            target = p / lf
            if target.exists():
                if target.is_dir():
                    import shutil
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
                print(f"[BrowserManager] Cleaned stale profile lock: {lf}")
        except Exception:
            pass


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

        # Delete stale lockfiles if they exist to prevent profile lock warnings
        _cleanup_profile_locks(self._user_data_dir)

        if self._browser is not None:
            # Check if the existing browser's WebSocket is still alive.
            # IMPORTANT: Only treat as stale if the browser was previously started
            # (has a _cdp_client_root) but the WebSocket connection died.
            # A browser that was never started (no _cdp_client_root) is NOT stale —
            # it just hasn't been initialized by Agent.run() yet.
            try:
                has_been_started = getattr(self._browser, '_cdp_client_root', None) is not None
                if has_been_started and not self._browser.is_cdp_connected:
                    print("[BrowserManager] Stale browser detected (WebSocket closed). Recreating...")
                    try:
                        import asyncio as _aio
                        await _aio.wait_for(self._browser.close(), timeout=3.0)
                    except Exception:
                        pass
                    self._browser = None
            except Exception:
                # If we can't even check, assume it's dead
                print("[BrowserManager] Cannot verify browser state. Recreating...")
                self._browser = None

        if self._browser is None:
            self._browser = self._build_browser()
        return self._browser

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
