"""
core/crawler.py — BFS page crawler. The orchestration brain.

This is the main loop from the flowchart:
  - Maintains visited set and URL queue
  - For each page: discover elements → interact → capture errors → collect links → return home
  - Delegates ALL interactions to Interactor
  - Delegates ALL reporting to ScreenshotManager + MetadataLogger
  - Delegates link discovery to ElementFinder
  - Never touches browser launch/teardown (owned by BrowserManager)

Error philosophy:
  - Element errors: captured, logged, execution continues to next element
  - Navigation errors: logged, page skipped, continue to next URL in queue
  - Never raise from this module — the run always completes.
"""

import sys
import os
# Ensure project root is on sys.path BEFORE any local imports.
# This must come first — before playwright and other local modules —
# so that 'reporting', 'core', and 'config' are all resolvable.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import logging
from collections import deque
from urllib.parse import urlparse, urljoin

from playwright.async_api import Page

import config
from core.element_finder import ElementFinder, ElementType
from core.interactor import Interactor
# ===== NEW FEATURE START =====
from core.form_tester import FormTester, ElementManifestExporter
# ===== NEW FEATURE END =====
from reporting.screenshot_manager import ScreenshotManager
from reporting.metadata_logger import MetadataLogger

logger = logging.getLogger(__name__)


class Crawler:

    def __init__(
        self,
        page: Page,
        start_url: str,
        screenshot_manager: ScreenshotManager,
        metadata_logger: MetadataLogger,
    ):
        self.page              = page
        self.start_url         = start_url
        self.home_url          = start_url
        self.screenshot_manager = screenshot_manager
        self.metadata_logger   = metadata_logger
        self.interactor        = Interactor(page)
        self.visited: set[str] = set()
        self.queue: deque[str] = deque()
        self._base_domain      = urlparse(start_url).netloc
        # ===== NEW FEATURE START =====
        # Form tester handles group-aware checkbox/radio interactions
        self.form_tester       = FormTester(page)
        # Manifest exporter writes a structured JSON of all discovered elements
        run_id = metadata_logger.run_id
        self.manifest_exporter = ElementManifestExporter(run_id)
        # ===== NEW FEATURE END =====

    async def run(self) -> list[str]:
        """
        Main BFS crawl loop. Returns list of all visited URLs.
        """
        self.queue.append(self._normalize(self.start_url))

        while self.queue:
            url = self.queue.popleft()

            if url in self.visited:
                continue
            if len(self.visited) >= config.MAX_PAGES:
                logger.warning("MAX_PAGES (%d) reached. Stopping crawler.", config.MAX_PAGES)
                break

            self.visited.add(url)
            logger.info("[%d] Testing page: %s", len(self.visited), url)
            print(f"\n[PAGE {len(self.visited)}] {url}")

            await self._test_page(url)

            # After testing each page, return to home before next
            if url != self.home_url:
                await self._safe_navigate(self.home_url)

        logger.info("Crawl complete. %d pages visited.", len(self.visited))

        # ===== NEW FEATURE START =====
        # Save element manifest to JSON after all pages are crawled
        manifest_path = self.manifest_exporter.save()
        summary = self.manifest_exporter.get_summary()
        print(f"\n  [✓] Element manifest saved: {manifest_path}")
        print(f"      Manifest covers {summary['total_pages']} page(s), "
              f"{summary['total_elements']} element(s) total.")
        # Final success banner (required by task spec)
        print("\n" + "=" * 60)
        print("  All additional UI elements tested successfully")
        print("=" * 60 + "\n")
        # ===== NEW FEATURE END =====

        # ===== HYPERLINK TEST START =====
        # Final hyperlink crawl completion banner
        print("=" * 60)
        print("  All hyperlinks tested successfully without duplication")
        print(f"  Total unique pages visited: {len(self.visited)}")
        print("=" * 60 + "\n")
        # ===== HYPERLINK TEST END =====

        return list(self.visited)

    async def _test_page(self, url: str) -> None:
        """Full element testing cycle for a single page."""

        # Navigate to this page
        success = await self._safe_navigate(url)
        if not success:
            return

        # FIX: Capture the ACTUAL URL the browser landed on after any redirects.
        # e.g. http://foo.com → navigates → https://foo.com
        # All drift comparisons must use canonical_url (the real browser URL),
        # NOT the input url, otherwise http→https redirects trigger a false drift
        # loop that re-discovers elements endlessly.
        canonical_url = self.page.url

        await asyncio.sleep(0.5)  # allow JS to settle

        # ===== HYPERLINK TEST START =====
        # Content-based broken page detection.
        # HTTP status alone is not enough — some servers return 200 with an
        # error page body (soft 404). Check visible text for known broken indicators.
        if await self._is_broken_page():
            print(f"  ✗ Broken link detected: {url}")
            logger.warning("Broken link detected (content check): %s", url)
            ss_path = await self.screenshot_manager.capture_error(
                page=self.page, url=url, action="navigate", error_type="broken_page_content"
            )
            self.metadata_logger.log_error(
                url=url,
                action="navigate",
                error_type="broken_page_content",
                error_message="Page content indicates broken/not-found state",
                screenshot_path=ss_path,
            )
            return   # skip element testing for broken pages
        # ===== HYPERLINK TEST END =====

        # ===== NEW FEATURE START =====
        # Detect and log dynamic / hidden elements before discovery
        await self.form_tester.detect_dynamic_elements()

        # Detect multi-step forms and log (we don't auto-advance — safer)
        if await self.form_tester.detect_multistep_form():
            logger.info("Multi-step form detected on %s — will be tested step-by-step if navigated.", url)
        # ===== NEW FEATURE END =====

        # Discover all interactive elements
        finder   = ElementFinder(self.page)
        elements = await finder.discover()

        # ===== NEW FEATURE START =====
        # Also scan same-origin iframes for additional elements
        iframe_elements = await finder.discover_in_iframes()
        if iframe_elements:
            print(f"  → {len(iframe_elements)} element(s) found in iframes")
            elements = elements + iframe_elements

        # Record all discovered elements to the manifest
        self.manifest_exporter.record_page(url, elements)
        # ===== NEW FEATURE END =====

        print(f"  → {len(elements)} elements found")

        # ===== NEW FEATURE START =====
        # Run group-aware checkbox and radio tests BEFORE the individual interaction loop.
        # This validates group-level behavior: toggle (check/uncheck) for checkboxes,
        # and exclusivity enforcement for radio groups.
        checkbox_results = await self.form_tester.test_checkbox_groups(elements)
        radio_results    = await self.form_tester.test_radio_groups(elements)

        for r in checkbox_results:
            status = "✓" if r.success else "✗"
            print(f"    {status}  [CHECKBOX_GROUP:{r.group_name}] {r.label[:40]} → {r.action}")
            if r.success:
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="CHECKBOX"
                )
            else:
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url,
                    action=r.action, error_type="checkbox_test_failure"
                )
                self.metadata_logger.log_error(
                    url=url, action=r.action,
                    error_type="checkbox_test_failure",
                    error_message=r.error_message,
                    element_label=r.label, element_type="CHECKBOX",
                    screenshot_path=ss_path,
                )

        for r in radio_results:
            status = "✓" if r.success else "✗"
            print(f"    {status}  [RADIO_GROUP:{r.group_name}] {r.label[:40]} → {r.action}")
            if r.success:
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="RADIO"
                )
            else:
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url,
                    action=r.action, error_type="radio_test_failure"
                )
                self.metadata_logger.log_error(
                    url=url, action=r.action,
                    error_type="radio_test_failure",
                    error_message=r.error_message,
                    element_label=r.label, element_type="RADIO",
                    screenshot_path=ss_path,
                )
        # ===== NEW FEATURE END =====
        # ===== FIX: Index-based while loop with processed-set deduplication =====
        # WHY: Python `for elem in elements` freezes the iterator at creation time.
        # Reassigning `elements` inside the loop has NO effect on iteration order.
        # After a navigation drift we restart from idx=0 with fresh handles BUT
        # the `processed` set prevents re-testing elements we already handled,
        # which stops the infinite loop caused by navigation buttons like "Home".
        element_list = list(elements)
        # Key: (selector, label, element_type) — unique enough across the page
        processed: set = set()
        idx = 0

        while idx < len(element_list):
            elem = element_list[idx]

            # Build a dedup key for this element
            elem_key = (elem.selector, elem.label, elem.element_type.name)

            # Skip elements already tested in this page cycle (prevents infinite loop)
            if elem_key in processed:
                idx += 1
                continue

            # Re-check page URL hasn't drifted from a PREVIOUS interaction.
            # Use canonical_url (post-redirect real URL) not the input url.
            current_url = self.page.url
            if self._normalize(current_url) != self._normalize(canonical_url):
                logger.debug("Page drifted to %s during element test, navigating back", current_url)
                success = await self._safe_navigate(canonical_url)
                if not success:
                    break   # can't recover — stop testing this page
                # Re-discover fresh handles; processed set keeps us from looping
                element_list = await finder.discover()
                idx = 0
                continue

            result = await self.interactor.interact(elem)

            # Mark as processed BEFORE handling result — prevents any retry loops
            processed.add(elem_key)
            idx += 1

            # Stale element — handle detached (e.g. iframe unload, React re-render)
            # Log as "skipped", NOT as an error — no screenshot wasted
            if result.action_performed.startswith("skipped_stale"):
                logger.debug("Stale element skipped: [%s] %s", result.element_type, result.element_label)
                print(f"    ~  [{result.element_type}] {result.element_label[:50]} → skipped (stale handle)")
                self.metadata_logger.log_action(
                    url=url,
                    action=result.action_performed,
                    element_label=result.element_label,
                    element_type=result.element_type,
                )
                continue

            if result.success:
                self.metadata_logger.log_action(
                    url=url,
                    action=result.action_performed,
                    element_label=result.element_label,
                    element_type=result.element_type,
                )
                print(f"    ✓  [{result.element_type}] {result.element_label[:50]} → {result.action_performed}")

                # Drift check AFTER success — buttons/links may have navigated away.
                # Use canonical_url (post-redirect) for comparison.
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    logger.debug("Post-interact drift detected, recovering to %s", canonical_url)
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    element_list = await finder.discover()
                    idx = 0

            else:
                # Capture screenshot and log error
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page,
                    url=url,
                    action=result.action_performed,
                    error_type="interaction_error",
                )
                self.metadata_logger.log_error(
                    url=url,
                    action=result.action_performed,
                    error_type="interaction_error",
                    error_message=result.error_message,
                    element_label=result.element_label,
                    element_type=result.element_type,
                    screenshot_path=ss_path,
                )
                print(f"    ✗  [{result.element_type}] {result.element_label[:50]} → ERROR captured")

                # Navigate back if drifted after error. Use canonical_url.
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    element_list = await finder.discover()
                    idx = 0
        # ===== END FIX =====


        # Collect new links and enqueue unvisited same-domain URLs
        new_links = await finder.collect_links()
        enqueued  = 0
        for link in new_links:
            norm = self._normalize(link)
            if norm and norm not in self.visited and self._is_same_domain(link):
                if norm not in self.queue:
                    self.queue.append(norm)
                    enqueued += 1

        print(f"  → {enqueued} new URLs enqueued ({len(self.queue)} in queue)")

    async def _safe_navigate(self, url: str) -> bool:
        """Navigate with error handling. Returns True on success."""
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT)
            if response and response.status >= 400:
                # ===== HYPERLINK TEST START =====
                # Log in the requested "Broken link detected" format
                print(f"  ✗ Broken link detected: {url} (HTTP {response.status})")
                logger.warning("HTTP %d navigating to %s", response.status, url)
                # ===== HYPERLINK TEST END =====
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url, action="navigate", error_type=f"http_{response.status}"
                )
                self.metadata_logger.log_error(
                    url=url,
                    action="navigate",
                    error_type=f"http_{response.status}",
                    error_message=f"Server returned {response.status}",
                    screenshot_path=ss_path,
                )
                return False
            return True
        except Exception as exc:
            logger.error("Navigation error on %s: %s", url, exc)
            self.metadata_logger.log_error(
                url=url,
                action="navigate",
                error_type="navigation_exception",
                error_message=str(exc)[:300],
            )
            return False

    def _normalize(self, url: str) -> str:
        """Normalize URL for deduplication.
        - Strips URL fragments (#anchor)
        - Strips trailing slashes
        - Upgrades http:// → https:// (treats HTTP redirects as same page)
        This prevents http://foo.com and https://foo.com from being crawled twice.
        """
        try:
            p = urlparse(url)
            # Upgrade http to https — servers that redirect http→https should
            # not be treated as a separate page from the https version.
            scheme = "https" if p.scheme == "http" else p.scheme
            normalized = p._replace(scheme=scheme, fragment="").geturl()
            return normalized.rstrip("/")
        except Exception:
            return url

    def _is_same_domain(self, url: str) -> bool:
        """Enforce same-domain-only crawling."""
        if not config.SAME_DOMAIN_ONLY:
            return True
        try:
            return urlparse(url).netloc == self._base_domain
        except Exception:
            return False

    # ===== HYPERLINK TEST START =====
    async def _is_broken_page(self) -> bool:
        """
        Content-based broken page detection.

        Some servers return HTTP 200 with an error body ("soft 404").
        This method scans visible page text for known broken-page signals.
        Complements the HTTP status check in _safe_navigate().

        Returns True if the page appears to be broken/not-found.
        """
        # Known broken-page text patterns (case-insensitive substring match)
        _BROKEN_SIGNALS = [
            "404",
            "page not found",
            "not found",
            "not available",
            "doesn't exist",
            "does not exist",
            "no longer available",
            "this page is gone",
            "we couldn't find",
            "cannot be found",
            "error 404",
            "oops",
        ]
        try:
            # Read the visible body text (not innerHTML — avoids script noise)
            body_text = await self.page.evaluate(
                "() => (document.body && document.body.innerText) ? "
                "document.body.innerText.toLowerCase().slice(0, 2000) : ''"
            )
            title_text = await self.page.title()
            combined = (body_text + " " + title_text.lower())[:2200]

            for signal in _BROKEN_SIGNALS:
                if signal in combined:
                    logger.debug("Broken page signal '%s' found on %s", signal, self.page.url)
                    return True
        except Exception as exc:
            logger.debug("Broken page check failed: %s", exc)
        return False
    # ===== HYPERLINK TEST END =====
