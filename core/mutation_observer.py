"""
core/mutation_observer.py — Lightweight JS MutationObserver wrapper.

Purpose:
  After EVERY click interaction, we want to know whether anything actually
  changed in the DOM (new nodes, attribute changes, URL change, etc.).
  If nothing changed → the click was a DEAD_CLICK.

Design:
  - arm()   : inject a tiny MutationObserver into the page BEFORE the click.
  - check() : evaluate the mutation count AFTER the click (within timeout).
  - Both methods are safe wrappers — never raise, never block the crawl.

Usage (inside interactor._click):
    observer = DOMObserver(page)
    await observer.arm()
    await handle.click()
    result = await observer.check(timeout_ms=800)
    if result.is_dead_click:
        logger.info("Dead click detected on '%s'", label)

No external dependencies — pure Playwright page.evaluate().
"""

import asyncio
import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ── JS payload injected before each click ─────────────────────────────────────
# Attaches a MutationObserver to document.body, counting any subtree change.
# Also records the URL at arm-time to detect navigation.
_ARM_JS = """
() => {
    window.__wat_mutations = 0;
    window.__wat_arm_url   = window.location.href;

    // Disconnect any previous observer to avoid stacking
    if (window.__wat_observer) {
        try { window.__wat_observer.disconnect(); } catch (_) {}
    }

    window.__wat_observer = new MutationObserver((records) => {
        window.__wat_mutations += records.length;
    });

    const target = document.body || document.documentElement;
    window.__wat_observer.observe(target, {
        childList:  true,
        subtree:    true,
        attributes: true,
        characterData: false,   // skip text-only flicker
    });
}
"""

# ── JS payload evaluated after the click ──────────────────────────────────────
# Returns {mutations: N, url_changed: bool}
_CHECK_JS = """
() => {
    const mutations   = window.__wat_mutations  || 0;
    const armUrl      = window.__wat_arm_url    || '';
    const urlChanged  = window.location.href !== armUrl;

    // Disconnect observer now — we have our reading
    if (window.__wat_observer) {
        try { window.__wat_observer.disconnect(); } catch (_) {}
    }
    window.__wat_observer  = null;
    window.__wat_mutations = 0;

    return { mutations, url_changed: urlChanged };
}
"""


class DOMChangeResult:
    """Encapsulates the outcome of a DOM-change check after a click."""

    def __init__(self, mutations: int, url_changed: bool, error: str = ""):
        self.mutations   = mutations
        self.url_changed = url_changed
        self.error       = error

    @property
    def is_dead_click(self) -> bool:
        """True when nothing changed — no DOM mutations, no URL change."""
        return self.mutations == 0 and not self.url_changed and not self.error

    @property
    def had_dom_change(self) -> bool:
        return self.mutations > 0

    def __repr__(self) -> str:
        return (
            f"DOMChangeResult(mutations={self.mutations}, "
            f"url_changed={self.url_changed}, "
            f"dead={self.is_dead_click})"
        )


class DOMObserver:
    """
    Lightweight wrapper around a JS MutationObserver for dead-click detection.

    Usage:
        observer = DOMObserver(page)
        await observer.arm()
        # … perform click …
        result = await observer.check(timeout_ms=800)
        if result.is_dead_click:
            # log or report
    """

    def __init__(self, page: Page):
        self._page  = page
        self._armed = False

    async def arm(self) -> bool:
        """
        Inject the MutationObserver into the page.
        MUST be called immediately before the click action.
        Returns True on success.
        """
        try:
            await self._page.evaluate(_ARM_JS)
            self._armed = True
            return True
        except Exception as exc:
            logger.debug("[DOMObserver] arm() failed: %s", exc)
            self._armed = False
            return False

    async def check(self, timeout_ms: int = 800) -> DOMChangeResult:
        """
        Wait `timeout_ms` for mutations to accumulate, then read the counter.
        Safe to call even if arm() was never called or failed.

        Returns a DOMChangeResult (never raises).
        """
        if not self._armed:
            # arm() was not called or failed — cannot determine dead click
            return DOMChangeResult(mutations=1, url_changed=False,
                                   error="observer_not_armed")

        # Brief yield to allow synchronous DOM mutations to fire before reading
        await asyncio.sleep(timeout_ms / 1000)

        try:
            data = await self._page.evaluate(_CHECK_JS)
            return DOMChangeResult(
                mutations=data.get("mutations", 0),
                url_changed=data.get("url_changed", False),
            )
        except Exception as exc:
            logger.debug("[DOMObserver] check() failed: %s", exc)
            # If page navigated away, evaluate() will fail — treat as url_changed
            return DOMChangeResult(mutations=0, url_changed=True,
                                   error=str(exc)[:100])
        finally:
            self._armed = False
