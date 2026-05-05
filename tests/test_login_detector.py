"""
tests/test_login_detector.py — Unit tests for LoginDetector.

Uses Playwright's sync API with a local mock HTML page.
No live browser required for CI — mocked via page.set_content().
"""

import pytest
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright


@pytest.mark.asyncio
async def test_detects_password_field():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html><body>
              <form>
                <input type="text" name="username"/>
                <input type="password" name="password"/>
                <button type="submit">Login</button>
              </form>
            </body></html>
        """)

        from core.login_detector import LoginDetector
        detector = LoginDetector(page)
        result = await detector.is_login_page()

        assert result is True
        await browser.close()


@pytest.mark.asyncio
async def test_no_false_positive_on_normal_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html><body>
              <h1>Welcome to Dashboard</h1>
              <input type="text" placeholder="Search..."/>
              <button>Go</button>
            </body></html>
        """)

        from core.login_detector import LoginDetector
        detector = LoginDetector(page)
        result = await detector.is_login_page()

        assert result is False
        await browser.close()


@pytest.mark.asyncio
async def test_detects_login_url_keyword():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # Override URL via route
        await page.route("**/login", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<html><body><form><input type='text'/></form></body></html>"
        ))

        from core.login_detector import LoginDetector
        # Test URL-based detection directly
        detector = LoginDetector(page)
        assert detector._url_suggests_login("https://example.com/login") is True
        assert detector._url_suggests_login("https://example.com/signin") is True
        assert detector._url_suggests_login("https://example.com/dashboard") is False

        await browser.close()
