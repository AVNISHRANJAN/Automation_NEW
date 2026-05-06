"""
core/browser.py — Playwright browser wrapper.

Responsibilities:
  - Launch / teardown browser lifecycle
  - Expose a single Page object to callers
  - Apply all timeouts from config centrally
  - Never make navigation decisions — that belongs to crawler.py
"""

import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

import sys
import os
# ===== OPTIMIZATION START =====
# Guard added: matches the pattern in all other modules — avoids re-inserting
# the project root on repeated imports (e.g. from test runners or reload).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# ===== OPTIMIZATION END =====
import config

logger = logging.getLogger(__name__)


class BrowserManager:
    """
    Owns the full Playwright lifecycle.
    Use as an async context manager:

        async with BrowserManager() as bm:
            page = await bm.new_page()
    """

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserManager":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.HEADLESS,
            slow_mo=config.SLOW_MO,
            args=["--start-maximized"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=False,
            ignore_https_errors=True,          # allow self-signed certs in test envs
        )
        self._context.set_default_timeout(config.ACTION_TIMEOUT)
        self._context.set_default_navigation_timeout(config.NAV_TIMEOUT)
        logger.info("Browser launched (headless=%s)", config.HEADLESS)
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed.")

    async def new_page(self) -> Page:
        """Return a new page in the shared context."""
        if not self._context:
            raise RuntimeError("BrowserManager not started. Use 'async with'.")
        page = await self._context.new_page()
        return page

    async def navigate(self, page: Page, url: str) -> bool:
        """
        Navigate to url. Returns True on success, False on navigation failure.
        Catches common Playwright navigation errors without crashing the run.
        """
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                logger.warning("HTTP %s on %s", response.status, url)
                return False
            return True
        except Exception as exc:
            logger.error("Navigation failed for %s: %s", url, exc)
            return False
