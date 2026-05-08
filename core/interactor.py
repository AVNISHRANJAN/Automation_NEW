"""
core/interactor.py — Element interaction engine.

Handles clicking, filling, selecting, and submitting.
Never crashes the run — all errors are caught, logged, and returned
as InteractionResult so the caller can decide what to record.

Design:
  - One public method: interact(element_info) → InteractionResult
  - Password fields are SKIPPED — we log a warning but never fill them
  - Form submits only happen after all fields in the form are filled
  - Selects choose the second option (index 1) to avoid default no-op
  - Dead-click detection: DOMObserver arms before click, checks after;
    if no DOM mutation + no URL change → result tagged dead_click=True
"""

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import logging
import asyncio
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

import config
from core.element_finder import ElementInfo, ElementType
# Dead-click detection — installed ONCE per Interactor instance
from core.mutation_observer import DOMObserver, DOMChangeResult

logger = logging.getLogger(__name__)

# ===== NEW FEATURE START =====
# Keywords that indicate a potentially destructive or session-ending action.
# Elements whose label or href contains these will be skipped.
_DESTRUCTIVE_KEYWORDS = [
    "logout", "log out", "sign out", "signout", "log-out", "sign-out",
    "delete", "remove", "cancel account", "deactivate", "terminate",
    "destroy", "purge", "wipe", "reset all", "clear all",
]
# ===== NEW FEATURE END =====

# Mobile/overlay navigation toggle buttons that are visually hidden on desktop
# but still discovered by Playwright.  Clicking them causes a 10-second timeout
# because the navbar intercepts pointer events.  Skip them immediately instead.
_OVERLAY_NAV_KEYWORDS = (
    "close menu", "open menu", "close nav", "open nav",
    "toggle menu", "toggle navigation", "toggle nav", "toggle sidebar",
    "hamburger", "menu toggle", "nav toggle",
)

# ===== OPTIMIZATION START =====
# Stale-handle error keywords — defined once here (was duplicated inside interact()).
# Tuple is faster than list for membership checks via 'any(kw in s for kw in ...)'.
_STALE_ERROR_KEYWORDS: tuple = (
    "not attached", "execution context", "target closed",
    "frame was detached", "object was collected",
)

# Dummy value pools — lifted to module level so they are allocated ONCE,
# not on every _get_dummy_value() call (which fires for every fillable element).
_DUMMY_NAMES    = ("Alice Smith", "Bob Johnson", "Carol White", "David Lee", "Test User")
_DUMMY_EMAILS   = ("alice@test.com", "bob@example.org", "qa_tester@mail.com", "tester@example.com")
_DUMMY_PHONES   = ("9876543210", "8765432109", "7654321098", "6543210987")
_DUMMY_CITIES   = ("Mumbai", "Delhi", "Bangalore", "Test City", "Chennai")
_DUMMY_MESSAGES = (
    "This is an automated test message.",
    "QA automation test input — please ignore.",
    "Testing form submission with sample data.",
)
_DUMMY_SEARCHES = ("test query", "sample search", "automation test", "demo")
# ===== OPTIMIZATION END =====


@dataclass
class InteractionResult:
    success: bool
    element_label: str
    element_type: str
    action_performed: str
    error_message: str = ""
    page_url_before: str = ""
    page_url_after: str = ""
    # NEW: set True when the click produced no DOM mutation and no URL change
    dead_click: bool = False
    # NEW: number of DOM mutations observed (0 on dead click)
    dom_mutations: int = 0


class Interactor:

    def __init__(self, page: Page):
        self.page = page
        # ===== FIX: Persistent dialog handler =====
        # Register ONE persistent dialog handler for the lifetime of this Interactor.
        self._last_dialog_type: str = ""
        self._last_dialog_message: str = ""
        self.page.on("dialog", self._global_dialog_handler)
        # ===== END FIX =====
        # Dead-click detector — shared across all _click() calls on this page
        self._observer = DOMObserver(page)

    async def _global_dialog_handler(self, dialog) -> None:
        """Persistent page-level dialog dismisser. Accepts all dialogs automatically."""
        try:
            self._last_dialog_type    = dialog.type
            self._last_dialog_message = dialog.message
            logger.info("Dialog intercepted [%s]: %s", dialog.type, dialog.message[:80])
            await dialog.accept()
        except Exception as exc:
            # Dialog was already handled (race condition) — safe to ignore
            logger.debug("Dialog already handled: %s", exc)

    async def interact(self, info: ElementInfo) -> InteractionResult:
        """
        Interact with a single element. Returns result regardless of success/failure.
        """
        url_before = self.page.url

        if info.element_type == ElementType.INPUT_PASS:
            # HARD RULE: Never autofill password fields
            logger.warning("Skipping password field: %s", info.label)
            return InteractionResult(
                success=True,
                element_label=info.label,
                element_type=info.element_type.name,
                action_performed="skipped_password_field",
                page_url_before=url_before,
                page_url_after=url_before,
            )

        # ===== FIX: Stale element pre-check =====
        # Detect detached / cross-origin iframe handles BEFORE dispatching.
        # These are not real interaction failures — the handle just became
        # invalid after a navigation or React/Vue re-render. Returning
        # "skipped_stale" lets the crawler restart with fresh handles cleanly.
        try:
            is_visible = await info.handle.is_visible()
            if not is_visible:
                return InteractionResult(
                    success=True,
                    element_label=info.label,
                    element_type=info.element_type.name,
                    action_performed="skipped_stale_not_visible",
                    page_url_before=url_before,
                    page_url_after=url_before,
                )
        except Exception as pre_exc:
            # ===== OPTIMIZATION START =====
            # Use shared _STALE_ERROR_KEYWORDS constant (no duplicate tuple literal)
            pre_msg = str(pre_exc).lower()
            if any(kw in pre_msg for kw in _STALE_ERROR_KEYWORDS):
            # ===== OPTIMIZATION END =====
                logger.debug("Stale handle detected for [%s] %s — skipping", info.element_type.name, info.label)
                return InteractionResult(
                    success=True,
                    element_label=info.label,
                    element_type=info.element_type.name,
                    action_performed="skipped_stale_detached",
                    page_url_before=url_before,
                    page_url_after=url_before,
                )
        # ===== END FIX =====

        try:
            action = await self._dispatch(info)
            await self.page.wait_for_load_state("domcontentloaded", timeout=config.ACTION_TIMEOUT)
            url_after = self.page.url

            return InteractionResult(
                success=True,
                element_label=info.label,
                element_type=info.element_type.name,
                action_performed=action,
                page_url_before=url_before,
                page_url_after=url_after,
            )

        except Exception as exc:
            error_msg = str(exc)
            # ===== FIX: Catch stale errors in dispatch too — return skip not fail =====
            # ===== OPTIMIZATION START =====
            # Reuse shared _STALE_ERROR_KEYWORDS constant (eliminates duplicate tuple)
            error_lower = error_msg.lower()
            if any(kw in error_lower for kw in _STALE_ERROR_KEYWORDS):
            # ===== OPTIMIZATION END =====
                logger.debug("Stale handle in dispatch [%s] %s — skipping", info.element_type.name, info.label)
                return InteractionResult(
                    success=True,
                    element_label=info.label,
                    element_type=info.element_type.name,
                    action_performed="skipped_stale_dispatch",
                    page_url_before=url_before,
                    page_url_after=self.page.url,
                )
            # ===== END FIX =====
            logger.error(
                "Interaction failed [%s | %s]: %s",
                info.element_type.name, info.label, error_msg[:300]
            )
            return InteractionResult(
                success=False,
                element_label=info.label,
                element_type=info.element_type.name,
                action_performed="failed",
                error_message=error_msg[:300],
                page_url_before=url_before,
                page_url_after=self.page.url,
            )


    async def _dispatch(self, info: ElementInfo) -> str:
        """Route to the correct interaction method based on element type."""

        t = info.element_type

        if t == ElementType.BUTTON:
            return await self._click(info)

        elif t in (ElementType.INPUT_TEXT, ElementType.INPUT_EMAIL,
                   ElementType.INPUT_TEL, ElementType.INPUT_NUMBER,
                   ElementType.INPUT_SEARCH, ElementType.TEXTAREA):
            return await self._fill(info)

        elif t == ElementType.SELECT:
            return await self._select(info)

        elif t == ElementType.CHECKBOX:
            return await self._check(info)

        elif t == ElementType.RADIO:
            return await self._click(info)

        elif t == ElementType.LINK:
            return await self._click_link(info)

        # ===== MANUAL FILE UPLOAD START =====
        elif t == ElementType.FILE_UPLOAD:
            return await self._handle_manual_file_upload(info)
        # ===== MANUAL FILE UPLOAD END =====

        elif t == ElementType.TAB:
            return await self._click_tab(info)

        # ===== Sidebar Detection Enhancement START =====
        elif t == ElementType.NAV_ITEM:
            # Sidebar/nav elements: treat as a safe click.
            # _click() already handles destructive keyword checks, tab cleanup,
            # dialog dismissal, and stale-handle protection.
            logger.info("[Sidebar Detection] SVG navigation element captured: %s", info.label)
            return await self._click(info)
        # ===== Sidebar Detection Enhancement END =====

        else:
            return await self._click(info)

    async def _click(self, info: ElementInfo) -> str:
        label_lower = info.label.lower()

        # Skip mobile nav toggle buttons (hamburger/close-menu) that are hidden
        # on desktop — clicking them causes a 10-second timeout because the
        # navbar intercepts pointer events on the real element underneath.
        if any(kw in label_lower for kw in _OVERLAY_NAV_KEYWORDS):
            logger.debug("Skipping overlay/mobile nav button: '%s'", info.label)
            return f"skipped_mobile_nav:{info.label}"

        # Skip buttons whose label looks destructive (logout, delete, etc.)
        if any(kw in label_lower for kw in _DESTRUCTIVE_KEYWORDS):
            logger.warning("Skipping destructive button: '%s'", info.label)
            return f"skipped_destructive:{info.label}"

        # Close any orphaned tabs left over from a PREVIOUS button's click.
        # This prevents a late-opening tab from being attributed to the wrong button.
        try:
            ctx = self.page.context
            orphaned = [p for p in ctx.pages if p != self.page]
            for orphan in orphaned:
                logger.debug("Closing orphaned tab before click: %s", orphan.url)
                await orphan.close()
        except Exception:
            pass

        # Reset dialog tracker before click so we know if THIS click triggered a dialog
        self._last_dialog_type = ""
        self._last_dialog_message = ""

        # Snapshot page count BEFORE clicking so we can detect newly opened tabs
        try:
            pages_before = len(self.page.context.pages)
        except Exception:
            pages_before = 1

        # ── Dead-click detection: arm MutationObserver BEFORE the click ────────
        await self._observer.arm()

        await info.handle.scroll_into_view_if_needed()
        await info.handle.click(timeout=config.ACTION_TIMEOUT)

        # Poll for up to 1.5s for a new tab to appear (more reliable than fixed sleep)
        new_tab_found = False
        for _ in range(6):   # 6 × 0.25s = 1.5s max wait
            await asyncio.sleep(0.25)
            try:
                current_pages = self.page.context.pages
                if len(current_pages) > pages_before:
                    new_tab_found = True
                    break
            except Exception:
                break

        # New-tab cleanup: close any extra pages opened by this button click
        try:
            context = self.page.context
            extra_pages = [p for p in context.pages if p != self.page]
            for extra in extra_pages:
                logger.info("Closing extra tab opened by button '%s': %s", info.label, extra.url)
                await extra.close()
            if extra_pages:
                return f"click_opened_tab:{len(extra_pages)}_tab(s)_closed"
        except Exception as tab_exc:
            logger.debug("Tab cleanup error: %s", tab_exc)

        if self._last_dialog_type:
            return f"click_dialog:{self._last_dialog_type}"

        # ── Dead-click detection: check AFTER the action ────────────────────────
        # Use DEAD_CLICK_TIMEOUT from config (default 800ms).
        # check() sleeps internally for the timeout duration, so the poll above
        # already consumed ~1.5s — we use 0ms here so it reads immediately.
        dom_result = await self._observer.check(timeout_ms=0)
        if dom_result.is_dead_click:
            logger.info(
                "[DeadClick] No DOM change detected after clicking '%s' — marking dead_click",
                info.label,
            )
            return f"dead_click:{info.label[:40]}"

        return "click"

    async def _fill(self, info: ElementInfo) -> str:
        dummy = self._get_dummy_value(info)
        await info.handle.scroll_into_view_if_needed()
        await info.handle.click()
        await info.handle.fill(dummy, timeout=config.ACTION_TIMEOUT)
        return f"fill:{dummy}"

    # ===== MANUAL FILE UPLOAD START =====
    async def _handle_manual_file_upload(self, info: ElementInfo) -> str:
        """
        Manual file upload handler.

        Opens the OS file chooser dialog by clicking the file input, then
        waits for the user to select a file (up to MANUAL_UPLOAD_TIMEOUT seconds).
        Uses event-based polling: checks el.files.length > 0 every 0.5s.

        Does NOT use set_input_files() or any automated file path injection.
        """
        MANUAL_UPLOAD_TIMEOUT = 60   # seconds to wait for user action
        POLL_INTERVAL         = 0.5  # seconds between file-selected checks

        is_multiple = await info.handle.get_attribute("multiple") is not None
        upload_type = "multi-file" if is_multiple else "single-file"

        print("\n  📂 Waiting for manual file upload (" + upload_type + "): '" + info.label + "'")
        print("     Please select a file in the browser dialog window.")
        logger.info("Waiting for manual file upload on '%s' (%s)", info.label, upload_type)

        try:
            await info.handle.scroll_into_view_if_needed()
            # Click the file input to trigger the OS file chooser dialog.
            # In headed (non-headless) mode this opens the native file dialog.
            await info.handle.click(timeout=config.ACTION_TIMEOUT)
        except Exception as exc:
            logger.warning("Could not click file input '%s': %s", info.label, exc)
            print(f"  ⚠  Could not open file dialog for '{info.label}'")
            return "file_upload:click_failed"

        # Poll until user selects file(s) or timeout expires
        elapsed = 0.0
        while elapsed < MANUAL_UPLOAD_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            try:
                # Check how many files the user has selected
                files_count = await self.page.evaluate(
                    "el => el.files ? el.files.length : 0",
                    info.handle
                )
                if files_count > 0:
                    # Read the selected filename(s) for logging
                    filenames = await self.page.evaluate(
                        "el => Array.from(el.files).map(f => f.name)",
                        info.handle
                    )
                    names_str = ", ".join(filenames)
                    print(f"  ✓  File selected successfully: {names_str}")
                    logger.info("File selected by user on '%s': %s", info.label, names_str)
                    return f"file_upload:manual_selected({names_str})"
            except Exception as poll_exc:
                # Handle stale element during polling (e.g. DOM update)
                logger.debug("File poll error on '%s': %s", info.label, poll_exc)
                break

        # Timeout reached — do not block execution
        print(f"  ⚠  Manual file upload skipped or timed out ({MANUAL_UPLOAD_TIMEOUT}s): '{info.label}'")
        logger.warning("Manual file upload timed out on '%s'", info.label)
        return "file_upload:skipped_timeout"

    async def _handle_auto_file_upload(self, info: ElementInfo) -> str:
        """
        Automated file upload using a generated temp file.
        Kept for reference — not called in normal flow (manual upload is used instead).
        Uses set_input_files() which bypasses the OS file dialog entirely.
        """
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="auto_test_upload_",
                delete=False, encoding="utf-8",
            ) as tmp:
                tmp.write("Automated test upload file\nGenerated by Web Auto Tester\n")
                tmp_path = tmp.name
            await info.handle.scroll_into_view_if_needed()
            await info.handle.set_input_files(tmp_path)
            logger.info("Auto-upload to '%s': %s", info.label, tmp_path)
            return f"file_upload:auto({Path(tmp_path).name})"
        except Exception as exc:
            raise RuntimeError(f"Auto file upload failed: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
    # ===== MANUAL FILE UPLOAD END =====


    async def _click_tab(self, info: ElementInfo) -> str:
        """
        Click a UI tab ([role=tab]) and wait briefly for the tab panel to activate.
        Tabs do not navigate pages — they reveal hidden content within the page.
        """
        await info.handle.scroll_into_view_if_needed()
        await info.handle.click(timeout=config.ACTION_TIMEOUT)
        await asyncio.sleep(0.4)   # allow tab panel content to render
        logger.info("Tab activated: '%s'", info.label)
        return f"tab_click:{info.label}"
    # ===== NEW CODE END =====

    async def _select(self, info: ElementInfo) -> str:
        """Select the second option to avoid blank/default no-ops."""
        try:
            options = await self.page.evaluate(
                "el => Array.from(el.options).map(o => o.value)",
                info.handle
            )
            if len(options) > 1:
                value = options[1]
            elif options:
                value = options[0]
            else:
                return "select:no_options"

            await info.handle.select_option(value=value, timeout=config.ACTION_TIMEOUT)
            return f"select:{value}"
        except Exception as exc:
            raise RuntimeError(f"Select failed: {exc}") from exc

    async def _check(self, info: ElementInfo) -> str:
        is_checked = await info.handle.is_checked()
        if not is_checked:
            await info.handle.check(timeout=config.ACTION_TIMEOUT)
        return "check"

    async def _click_link(self, info: ElementInfo) -> str:
        """
        For links, we don't actually navigate — the crawler controls navigation.
        We just register that we found and processed this link.
        """
        # ===== NEW FEATURE START =====
        # Skip links that look like logout or destructive actions
        label_lower = info.label.lower()
        href_lower  = info.href.lower()
        if any(kw in label_lower or kw in href_lower for kw in _DESTRUCTIVE_KEYWORDS):
            logger.warning("Skipping destructive link: '%s' (%s)", info.label, info.href)
            return f"skipped_destructive_link:{info.href}"
        # ===== NEW FEATURE END =====
        return f"link_recorded:{info.href}"

    def _get_dummy_value(self, info: ElementInfo) -> str:
        """Pick the most appropriate dummy value based on element context.
        Uses randomised pools so each test run uses different values.
        """
        # ===== OPTIMIZATION START =====
        # Pools are now module-level constants (allocated once, not per-call).
        # ===== OPTIMIZATION END =====
        label_lower = info.label.lower()
        input_type  = info.input_type.lower()

        if input_type == "email" or "email" in label_lower:
            # Prefer canonical configured email to make test behaviour deterministic
            return config.DUMMY_DATA.get("email", random.choice(_DUMMY_EMAILS))
        if input_type == "tel" or "phone" in label_lower or "mobile" in label_lower:
            return random.choice(_DUMMY_PHONES)
        if input_type == "number" or "zip" in label_lower or "postal" in label_lower:
            return config.DUMMY_DATA["zip"]
        if "name" in label_lower:
            return random.choice(_DUMMY_NAMES)
        if "address" in label_lower:
            return config.DUMMY_DATA["address"]
        if "city" in label_lower:
            return random.choice(_DUMMY_CITIES)
        if "search" in label_lower or input_type == "search":
            return random.choice(_DUMMY_SEARCHES)
        if "message" in label_lower or "comment" in label_lower or info.element_type == ElementType.TEXTAREA:
            return random.choice(_DUMMY_MESSAGES)
        if "user" in label_lower:
            return config.DUMMY_DATA["username"]
        if input_type == "url" or "website" in label_lower or "url" in label_lower:
            return config.DUMMY_DATA.get("url", "https://example.com")
        if input_type == "date" or "date" in label_lower:
            return config.DUMMY_DATA.get("date", "2025-01-15")
        if input_type == "time" or "time" in label_lower:
            return config.DUMMY_DATA.get("time", "10:30")
        if input_type == "range":
            return config.DUMMY_DATA.get("range", "50")

        return config.DUMMY_DATA["default"]
