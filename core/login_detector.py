"""
core/login_detector.py — Login page detection and manual credential handoff.

Design decisions:
  - NEVER touch, read, or autofill credentials. Zero credential exposure.
  - Detection uses DOM heuristics + URL keyword matching (both checked).
  - After detecting login, we wait for the URL to change OR the password
    field to disappear — whichever comes first — with a generous timeout.
  - Prints a clear human-readable prompt to terminal.
"""

import logging
from urllib.parse import urlparse
from playwright.async_api import Page

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class LoginDetector:

    def __init__(self, page: Page):
        self.page = page

    async def is_login_page(self) -> bool:
        """
        Returns True if the current page looks like a login page.
        Checks:
          1. URL contains login-related keywords
          2. DOM has a password input field
          3. DOM matches known login form patterns
        """
        url = self.page.url
        if self._url_suggests_login(url):
            logger.debug("Login detected via URL: %s", url)
            return True

        for selector in config.LOGIN_SELECTORS:
            try:
                element = await self.page.query_selector(selector)
                if element:
                    logger.debug("Login detected via selector: %s", selector)
                    return True
            except Exception:
                continue

        return False

    def _url_suggests_login(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        query = urlparse(url).query.lower()
        combined = path + query
        return any(kw in combined for kw in config.LOGIN_URL_KEYWORDS)

    async def wait_for_manual_login(self, original_url: str) -> bool:
        """
        Pause execution. Print a clear prompt. Wait for:
          - URL to change from the login page (user navigated after login), OR
          - Password input to disappear from DOM (SPA login flow)

        Returns True if login appears successful, False if timeout.
        """
        print("\n" + "=" * 60)
        print("  LOGIN REQUIRED")
        print("=" * 60)
        print(f"  URL: {self.page.url}")
        print("  Please log in manually in the browser window.")
        print("  The test will resume automatically once you are logged in.")
        print("=" * 60 + "\n")

        timeout_ms = config.POST_LOGIN_WAIT_TIMEOUT

        try:
            # Strategy 1: Wait for URL to change away from login page
            await self.page.wait_for_function(
                """(originalUrl) => {
                    const cur = window.location.href;
                    return cur !== originalUrl && !cur.includes('login') && !cur.includes('signin');
                }""",
                arg=original_url,
                timeout=timeout_ms,
            )
            logger.info("Login detected: URL changed to %s", self.page.url)
            print(f"\n[✓] Login successful. Resuming test on: {self.page.url}\n")
            return True

        except Exception:
            # Strategy 2: Check if password field disappeared (SPA flows)
            try:
                await self.page.wait_for_selector(
                    'input[type="password"]',
                    state="detached",
                    timeout=5000,
                )
                logger.info("Login detected: password field removed from DOM")
                print(f"\n[✓] Login successful. Resuming test on: {self.page.url}\n")
                return True
            except Exception:
                pass

        logger.warning("Manual login timeout after %dms", timeout_ms)
        print("\n[!] Login timeout. Proceeding without authentication.\n")
        return False
