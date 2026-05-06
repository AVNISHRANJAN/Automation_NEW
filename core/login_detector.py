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
from reporting import console

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
        # ===== OPTIMIZATION START =====
        # Parse URL once; was calling urlparse(url) twice to get path and query separately.
        parsed   = urlparse(url)
        combined = parsed.path.lower() + parsed.query.lower()
        # ===== OPTIMIZATION END =====
        return any(kw in combined for kw in config.LOGIN_URL_KEYWORDS)

    async def wait_for_manual_login(self, original_url: str) -> bool:
        """
        Pause execution. Print a clear prompt. Wait for:
          - URL to change from the login page (user navigated after login), OR
          - Password input to disappear from DOM (SPA login flow)

        Returns True if login appears successful, False if timeout.
        """
        console.print_login_required(self.page.url)

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
            console.print_login_success(self.page.url)
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
                console.print_login_success(self.page.url)
                return True
            except Exception:
                pass

        logger.warning("Manual login timeout after %dms", timeout_ms)
        console.print_login_timeout()
        return False
