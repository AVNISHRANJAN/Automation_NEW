"""
tests/test_interactor.py — Unit tests for Interactor.

Verifies correct dummy data selection and that password fields are never filled.
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from core.element_finder import ElementType
from core.interactor import Interactor


@pytest.mark.asyncio
async def test_fill_email_field():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<html><body><input type="email" id="em"/></body></html>')

        interactor = Interactor(page)
        handle = await page.query_selector("#em")

        from core.element_finder import ElementInfo
        info = ElementInfo(
            element_type=ElementType.INPUT_EMAIL,
            selector='#em',
            handle=handle,
            label="email",
            input_type="email",
        )
        result = await interactor.interact(info)

        assert result.success is True
        value = await page.input_value("#em")
        assert "example.com" in value   # dummy email used
        await browser.close()


@pytest.mark.asyncio
async def test_password_field_is_never_filled():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<html><body><input type="password" id="pw"/></body></html>')

        interactor = Interactor(page)
        handle = await page.query_selector("#pw")

        from core.element_finder import ElementInfo
        info = ElementInfo(
            element_type=ElementType.INPUT_PASS,
            selector='#pw',
            handle=handle,
            label="password",
            input_type="password",
        )
        result = await interactor.interact(info)

        # Must succeed (no error) but must NOT have filled the field
        assert result.success is True
        assert result.action_performed == "skipped_password_field"
        value = await page.input_value("#pw")
        assert value == ""   # empty — never touched
        await browser.close()


@pytest.mark.asyncio
async def test_select_picks_second_option():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html><body>
              <select id="sel">
                <option value="">-- select --</option>
                <option value="opt1">Option 1</option>
                <option value="opt2">Option 2</option>
              </select>
            </body></html>
        """)

        interactor = Interactor(page)
        handle = await page.query_selector("#sel")

        from core.element_finder import ElementInfo
        info = ElementInfo(
            element_type=ElementType.SELECT,
            selector='#sel',
            handle=handle,
            label="select",
        )
        result = await interactor.interact(info)

        assert result.success is True
        value = await page.input_value("#sel")
        assert value == "opt1"   # second option (index 1) selected
        await browser.close()
