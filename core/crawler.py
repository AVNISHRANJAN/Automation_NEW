"""
core/crawler.py — BFS page crawler. The orchestration brain.

This is the main loop from the flowchart:
  - Maintains visited set and URL queue
  - For each page: discover elements → interact → capture errors → collect links → return home
  - Delegates ALL interactions to Interactor
  - Delegates ALL reporting to ScreenshotManager + MetadataLogger
  - Delegates link discovery to ElementFinder
  - Never touches browser launch/teardown (owned by BrowserManager)

Terminal output:
  - All user-facing output goes through reporting.console (structured, coloured).
  - logger.* calls go to the log FILE only (terminal handler is WARNING+).
  - No raw print() calls in this module.

Error philosophy:
  - Element errors: captured, logged, execution continues to next element
  - Navigation errors: logged, page skipped, continue to next URL in queue
  - Never raise from this module — the run always completes.
"""

import sys
import os
# Ensure project root is on sys.path BEFORE any local imports.
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
from core.form_tester import FormTester, ElementManifestExporter
from reporting.screenshot_manager import ScreenshotManager
from reporting.metadata_logger import MetadataLogger
from reporting import console

logger = logging.getLogger(__name__)

# ── Module-level constants ─────────────────────────────────────────────────────

# Broken-page text signals — allocated ONCE, not on every _is_broken_page() call.
_BROKEN_SIGNALS: tuple = (
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
)

# Element types handled exclusively by form_tester (not re-tested in interactor loop).
_FORM_TESTER_HANDLED = frozenset({ElementType.CHECKBOX, ElementType.RADIO})

# Mapping from ElementType to display section name for grouped terminal output.
_SECTION_LABELS: dict = {
    ElementType.BUTTON:       "BUTTONS",
    ElementType.INPUT_TEXT:   "INPUTS",
    ElementType.INPUT_EMAIL:  "INPUTS",
    ElementType.INPUT_TEL:    "INPUTS",
    ElementType.INPUT_NUMBER: "INPUTS",
    ElementType.INPUT_SEARCH: "INPUTS",
    ElementType.INPUT_PASS:   "INPUTS",
    ElementType.TEXTAREA:     "INPUTS",
    ElementType.SELECT:       "INPUTS",
    ElementType.CHECKBOX:     "FORMS",
    ElementType.RADIO:        "FORMS",
    ElementType.LINK:         "LINKS",
    ElementType.FILE_UPLOAD:  "FILE_UPLOADS",
    ElementType.TAB:          "TABS",
    ElementType.FORM:         "FORMS",
    ElementType.OTHER:        "OTHER",
}


class Crawler:

    def __init__(
        self,
        page: Page,
        start_url: str,
        screenshot_manager: ScreenshotManager,
        metadata_logger: MetadataLogger,
    ):
        self.page               = page
        self.start_url          = start_url
        self.home_url           = start_url
        self.screenshot_manager = screenshot_manager
        self.metadata_logger    = metadata_logger
        self.interactor         = Interactor(page)
        self.visited: set[str]  = set()
        self.queue: deque[str]  = deque()
        self._base_domain       = urlparse(start_url).netloc
        self.form_tester        = FormTester(page)
        run_id = metadata_logger.run_id
        self.manifest_exporter  = ElementManifestExporter(run_id)

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
            console.print_page_header(len(self.visited), url)

            await self._test_page(url)

            # After testing each page, return home before next
            if url != self.home_url:
                await self._safe_navigate(self.home_url)

        logger.info("Crawl complete. %d pages visited.", len(self.visited))

        # Save element manifest
        manifest_path = self.manifest_exporter.save()
        summary       = self.manifest_exporter.get_summary()
        console.print_manifest_saved(
            manifest_path,
            summary["total_pages"],
            summary["total_elements"],
        )

        console.print_crawl_complete(len(self.visited))

        return list(self.visited)

    async def _test_page(self, url: str) -> None:
        """Full element testing cycle for a single page."""

        success = await self._safe_navigate(url)
        if not success:
            return

        # Capture ACTUAL URL after redirects (e.g. http→https)
        canonical_url = self.page.url

        await asyncio.sleep(0.5)  # allow JS to settle

        # Content-based broken page detection (soft 404s)
        if await self._is_broken_page():
            console.print_broken_link(url, "content check")
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
            return

        # Detect dynamic / hidden elements before discovery
        await self.form_tester.detect_dynamic_elements()

        # Detect multi-step forms (log only — no auto-advance)
        if await self.form_tester.detect_multistep_form():
            logger.info("Multi-step form detected on %s", url)

        # Discover all interactive elements
        finder   = ElementFinder(self.page)
        elements = await finder.discover()

        # Also scan same-origin iframes
        iframe_elements = await finder.discover_in_iframes()
        if iframe_elements:
            console.print_iframe_found(len(iframe_elements))
            elements = elements + iframe_elements

        # Record elements to manifest
        self.manifest_exporter.record_page(url, elements)

        # Print element count
        console.print_element_count(len(elements))

        # ── SECTION: FORMS (checkbox + radio via form_tester) ─────────────────
        page_errors: list[dict] = []   # collect per-page errors for the error block
        page_passed = 0
        page_failed = 0
        page_skipped = 0

        checkboxes = [e for e in elements if e.element_type == ElementType.CHECKBOX]
        radios     = [e for e in elements if e.element_type == ElementType.RADIO]

        if checkboxes or radios:
            console.print_section_header("FORMS")

        checkbox_results = await self.form_tester.test_checkbox_groups(elements)
        radio_results    = await self.form_tester.test_radio_groups(elements)

        for r in checkbox_results:
            console.print_form_group_result(r.success, "CHECKBOX_GROUP", r.group_name, r.label, r.action)
            if r.success:
                page_passed += 1
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="CHECKBOX"
                )
            else:
                page_failed += 1
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
                page_errors.append({"label": r.label, "action": r.action, "message": r.error_message})

        for r in radio_results:
            console.print_form_group_result(r.success, "RADIO_GROUP", r.group_name, r.label, r.action)
            if r.success:
                page_passed += 1
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="RADIO"
                )
            else:
                page_failed += 1
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
                page_errors.append({"label": r.label, "action": r.action, "message": r.error_message})

        # ── INTERACTOR LOOP with section grouping ──────────────────────────────
        # Index-based while loop with processed-set deduplication.
        # CHECKBOX and RADIO are skipped here — already handled by form_tester above.
        element_list = list(elements)
        processed: set = set()
        idx = 0

        # Track current section to print headers only when section changes
        current_section: str = ""

        while idx < len(element_list):
            elem = element_list[idx]

            # Skip types already handled by form_tester
            if elem.element_type in _FORM_TESTER_HANDLED:
                idx += 1
                continue

            # Build a dedup key
            elem_key = (elem.selector, elem.label, elem.element_type.name)

            if elem_key in processed:
                idx += 1
                continue

            # Re-check for page drift from a PREVIOUS interaction
            current_url = self.page.url
            if self._normalize(current_url) != self._normalize(canonical_url):
                logger.debug("Page drifted to %s, recovering to %s", current_url, canonical_url)
                success = await self._safe_navigate(canonical_url)
                if not success:
                    break
                element_list = await finder.discover()
                current_section = ""   # reset section on rediscover
                idx = 0
                continue

            # Print section header when the section changes
            section = _SECTION_LABELS.get(elem.element_type, "OTHER")
            if section != current_section:
                console.print_section_header(section)
                current_section = section

            result = await self.interactor.interact(elem)

            processed.add(elem_key)
            idx += 1

            # Stale element — skip cleanly
            if result.action_performed.startswith("skipped_stale"):
                page_skipped += 1
                logger.debug("Stale element skipped: [%s] %s", result.element_type, result.element_label)
                console.print_action(True, result.element_type, result.element_label, result.action_performed)
                self.metadata_logger.log_action(
                    url=url,
                    action=result.action_performed,
                    element_label=result.element_label,
                    element_type=result.element_type,
                )
                continue

            if result.success:
                page_passed += 1
                self.metadata_logger.log_action(
                    url=url,
                    action=result.action_performed,
                    element_label=result.element_label,
                    element_type=result.element_type,
                )
                console.print_action(True, result.element_type, result.element_label, result.action_performed)

                # Drift check AFTER success
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    logger.debug("Post-interact drift detected, recovering to %s", canonical_url)
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    element_list = await finder.discover()
                    current_section = ""
                    idx = 0

            else:
                page_failed += 1
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
                console.print_action(False, result.element_type, result.element_label, "ERROR captured")
                page_errors.append({
                    "label":   result.element_label,
                    "action":  result.action_performed,
                    "message": result.error_message[:80],
                })

                # Navigate back if drifted after error
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    element_list = await finder.discover()
                    current_section = ""
                    idx = 0

        # ── Per-page error block ───────────────────────────────────────────────
        if page_errors:
            console.print_error_block(page_errors)

        # ── Per-page summary ───────────────────────────────────────────────────
        console.print_page_summary(page_passed, page_failed, page_skipped)

        # Collect new links and enqueue unvisited same-domain URLs
        new_links = await finder.collect_links()
        enqueued  = 0
        for link in new_links:
            norm = self._normalize(link)
            if norm and norm not in self.visited and self._is_same_domain(link):
                if norm not in self.queue:
                    self.queue.append(norm)
                    enqueued += 1

        console.print_links_enqueued(enqueued, len(self.queue))

    async def _safe_navigate(self, url: str) -> bool:
        """Navigate with error handling. Returns True on success."""
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT)
            if response and response.status >= 400:
                console.print_broken_link(url, f"HTTP {response.status}")
                logger.warning("HTTP %d navigating to %s", response.status, url)
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
        """
        Normalize URL for deduplication.
        - Strips URL fragments (#anchor)
        - Strips trailing slashes
        - Upgrades http:// → https://
        """
        try:
            p = urlparse(url)
            scheme     = "https" if p.scheme == "http" else p.scheme
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

    async def _is_broken_page(self) -> bool:
        """
        Content-based broken page detection.
        Checks visible page text for known broken-page signals (soft 404s).
        Returns True if the page appears broken/not-found.
        """
        try:
            combined = await self.page.evaluate("""
                () => {
                    const body  = (document.body && document.body.innerText)
                                  ? document.body.innerText.toLowerCase().slice(0, 2000)
                                  : '';
                    const title = document.title ? document.title.toLowerCase() : '';
                    return (body + ' ' + title).slice(0, 2200);
                }
            """)
            for signal in _BROKEN_SIGNALS:
                if signal in combined:
                    logger.debug("Broken page signal '%s' found on %s", signal, self.page.url)
                    return True
        except Exception as exc:
            logger.debug("Broken page check failed: %s", exc)
        return False
