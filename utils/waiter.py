"""
utils/waiter.py — Playwright wait helpers.
Wraps common wait patterns with consistent timeout handling.
"""

import logging
from playwright.async_api import Page

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


async def wait_for_page_ready(page: Page, timeout: int = None) -> bool:
    """Wait for DOM content + network idle. Returns True on success."""
    t = timeout or config.NAV_TIMEOUT
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=t)
        return True
    except Exception as exc:
        logger.warning("wait_for_page_ready timeout: %s", exc)
        return False


async def wait_for_selector(page: Page, selector: str, timeout: int = None) -> bool:
    """Wait for a selector to appear. Returns True if found."""
    t = timeout or config.ACTION_TIMEOUT
    try:
        await page.wait_for_selector(selector, timeout=t)
        return True
    except Exception:
        return False


async def wait_for_url_change(page: Page, original_url: str, timeout: int = None) -> bool:
    """Wait until the page URL differs from original_url."""
    t = timeout or config.NAV_TIMEOUT
    try:
        await page.wait_for_function(
            "(orig) => window.location.href !== orig",
            arg=original_url,
            timeout=t,
        )
        return True
    except Exception:
        return False
