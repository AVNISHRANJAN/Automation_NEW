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
from core.security_scanner import SecurityScanner
from core.state_tracker import StateTracker
from reporting.screenshot_manager import ScreenshotManager
from reporting.metadata_logger import MetadataLogger
from reporting.ui_inventory import UIInventory
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

# START: Sidebar Navigation Processing
# NAV_ITEM is excluded from the main interactor loop and processed
# sequentially by _run_sidebar_navigation() after all other elements.
# This prevents sidebar clicks from disrupting mid-page form testing.
_SIDEBAR_HANDLED = frozenset({ElementType.NAV_ITEM})
# END: Sidebar Navigation Processing

# ACCORDION elements are tested by _run_accordion_testing() AFTER the main loop
# (they expand content, so they must not interleave with form/input testing).
_ACCORDION_HANDLED = frozenset({ElementType.ACCORDION})

# Mapping from ElementType to display section name for grouped terminal output.
_SECTION_LABELS: dict = {
    ElementType.BUTTON:         "BUTTONS",
    ElementType.INPUT_TEXT:     "INPUTS",
    ElementType.INPUT_EMAIL:    "INPUTS",
    ElementType.INPUT_TEL:      "INPUTS",
    ElementType.INPUT_NUMBER:   "INPUTS",
    ElementType.INPUT_SEARCH:   "INPUTS",
    ElementType.INPUT_PASS:     "INPUTS",
    ElementType.TEXTAREA:       "INPUTS",
    ElementType.SELECT:         "INPUTS",
    ElementType.CHECKBOX:       "FORMS",
    ElementType.RADIO:          "FORMS",
    ElementType.LINK:           "LINKS",
    ElementType.FILE_UPLOAD:    "FILE_UPLOADS",
    ElementType.TAB:            "TABS",
    ElementType.FORM:           "FORMS",
    ElementType.OTHER:          "OTHER",
    # ===== Sidebar Detection Enhancement START =====
    ElementType.NAV_ITEM:       "SIDEBAR_NAV",
    # ===== Sidebar Detection Enhancement END =====
    # ===== DYNAMIC UI ELEMENTS START =====
    ElementType.ACCORDION:      "ACCORDIONS",
    ElementType.MODAL_TRIGGER:  "MODAL_TRIGGERS",
    ElementType.CLICKABLE_CARD: "CARDS",
    # ===== DYNAMIC UI ELEMENTS END =====
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
        self._last_nav_response = None
        # ── State tracker: crawl-wide dedup for ALL elements/routes/states ──
        self.state_tracker      = StateTracker()
        self.form_tester        = FormTester(page, state_tracker=self.state_tracker)
        self.security_scanner   = SecurityScanner(page, run_id=metadata_logger.run_id, base_domain=self._base_domain)
        if config.SECURITY_SCAN_ENABLED:
            self.security_scanner.install_passive_hooks()
        run_id = metadata_logger.run_id
        self.manifest_exporter  = ElementManifestExporter(run_id)
        # START: Intelligent UI Analysis & Navigation Testing
        self.ui_inventory       = UIInventory(run_id, start_url)
        # END: Intelligent UI Analysis & Navigation Testing
        # GLOBAL dedup: NAV_ITEMs (navbar/sidebar) are the SAME on every page.
        # Track their fingerprints crawl-wide so they are tested only ONCE.
        # Also synced into state_tracker.tested_elements for unified dedup.
        self._global_nav_fingerprints: set = set()

    async def run(self) -> list[str]:
        """
        Main BFS crawl loop. Returns list of all visited URLs.
        """
        self.queue.append(self._normalize(self.start_url))

        while self.queue:
            url = self.queue.popleft()

            if url in self.visited:
                logger.debug("Skipping previously tested route: %s", url)
                continue
            if len(self.visited) >= config.MAX_PAGES:
                logger.warning("MAX_PAGES (%d) reached. Stopping crawler.", config.MAX_PAGES)
                break

            self.visited.add(url)
            self.state_tracker.mark_page_visited(url)   # sync route into StateTracker
            console.print_page_header(len(self.visited), url)

            await self._test_page(url)

            # After testing each page, return home before next
            if url != self.home_url:
                await self._safe_navigate(self.home_url)

        logger.info("Crawl complete. %d pages visited.", len(self.visited))
        dedup_summary = self.state_tracker.summary()
        logger.info(
            "State tracker summary — pages: %d | elements tested: %d | "
            "interactions: %d | UI states: %d",
            dedup_summary["visited_pages"],
            dedup_summary["tested_elements"],
            dedup_summary["tested_interactions"],
            dedup_summary["tested_states"],
        )

        # Save element manifest
        manifest_path = self.manifest_exporter.save()
        summary       = self.manifest_exporter.get_summary()
        console.print_manifest_saved(
            manifest_path,
            summary["total_pages"],
            summary["total_elements"],
        )

        # START: Intelligent UI Analysis & Navigation Testing
        # Save UI inventory (classified JSON grouped by functionality module)
        self.ui_inventory.save()
        # END: Intelligent UI Analysis & Navigation Testing

        console.print_crawl_complete(len(self.visited))

        return list(self.visited)

    async def _test_page(self, url: str) -> None:
        """Full element testing cycle for a single page."""

        # START: Intelligent Recursive Functional Testing
        logger.info("[Recursive] Full page inspection started: %s", url)
        # END: Intelligent Recursive Functional Testing

        success = await self._safe_navigate(url)
        if not success:
            return

        # Capture ACTUAL URL after redirects (e.g. http→https)
        canonical_url = self.page.url

        if config.SECURITY_SCAN_ENABLED:
            await self.security_scanner.scan_page(canonical_url, self._last_nav_response)
            await self.security_scanner.run_safe_input_probes(canonical_url)
            new_findings = self.security_scanner.export_new_findings()
            for finding in new_findings:
                if not finding.get("screenshot_path"):
                    shot = await self.screenshot_manager.capture_error(
                        page=self.page,
                        url=canonical_url,
                        action="security_scan",
                        error_type=str(finding.get("category", "security_finding")),
                    )
                    finding["screenshot_path"] = shot
            self.metadata_logger.log_security_findings(new_findings)

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

        # ===== Sidebar Detection Enhancement START =====
        # discover_sidebar_elements() runs a second targeted pass using
        # SIDEBAR_SELECTORS (div/span/li/SVG/onclick/aria-label nav patterns).
        # It shares finder._seen_handles so elements already found above are
        # NEVER duplicated — zero risk of double-testing.
        sidebar_elements = await finder.discover_sidebar_elements(finder._seen_handles)
        if sidebar_elements:
            logger.info("[Sidebar Detection] %d sidebar element(s) added to test set", len(sidebar_elements))
            elements = elements + sidebar_elements
        # ===== Sidebar Detection Enhancement END =====

        # ===== DYNAMIC UI ELEMENTS START =====
        # discover_dynamic_elements() finds ACCORDION / MODAL_TRIGGER / CLICKABLE_CARD
        # elements using DYNAMIC_SELECTORS.  Shares the same _seen_handles dedup set
        # so no element is counted twice.
        dynamic_elements = await finder.discover_dynamic_elements(finder._seen_handles)
        if dynamic_elements:
            logger.info("[DynamicUI] %d dynamic element(s) added to test set", len(dynamic_elements))
            elements = elements + dynamic_elements

        # discover_shadow_dom_elements() logs shadow-root-hosted elements for inventory
        # but does NOT add them to the interactive list (handles not accessible via Playwright).
        await finder.discover_shadow_dom_elements(finder._seen_handles)
        # ===== DYNAMIC UI ELEMENTS END =====

        # Record elements to manifest
        self.manifest_exporter.record_page(url, elements)

        # START: Intelligent UI Analysis & Navigation Testing
        # Ingest elements into UI inventory (enriches with xpath/parent/classification)
        # Called before the interaction loop so handles are still fresh.
        await self.ui_inventory.ingest(url, elements, self.page)
        # END: Intelligent UI Analysis & Navigation Testing

        # Print element count
        console.print_element_count(len(elements))

        # START: Intelligent Recursive Functional Testing
        logger.info("[Recursive] Testing current page functionality: %s", url)
        # END: Intelligent Recursive Functional Testing

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

        # ===== DROPDOWN TEST START =====
        dropdown_results = await self.form_tester.handleDynamicDropdowns()
        for r in dropdown_results:
            console.print_form_group_result(r.success, "DROPDOWN_CHAIN", r.group_name, r.label, r.action)
            if r.success:
                page_passed += 1
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="DROPDOWN"
                )
            else:
                page_failed += 1
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url,
                    action=r.action, error_type="dropdown_dependency_failure"
                )
                self.metadata_logger.log_error(
                    url=url, action=r.action,
                    error_type="dropdown_dependency_failure",
                    error_message=r.error_message,
                    element_label=r.label, element_type="DROPDOWN",
                    screenshot_path=ss_path,
                )
                page_errors.append({"label": r.label, "action": r.action, "message": r.error_message})

        action_btn_results = await self.form_tester.testActionButtons()
        for r in action_btn_results:
            console.print_form_group_result(r.success, "DROPDOWN_ACTIONS", r.group_name, r.label, r.action)
            if r.success:
                page_passed += 1
                self.metadata_logger.log_action(
                    url=url, action=r.action,
                    element_label=r.label, element_type="BUTTON"
                )
            else:
                page_failed += 1
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url,
                    action=r.action, error_type="dropdown_action_button_failure"
                )
                self.metadata_logger.log_error(
                    url=url, action=r.action,
                    error_type="dropdown_action_button_failure",
                    error_message=r.error_message,
                    element_label=r.label, element_type="BUTTON",
                    screenshot_path=ss_path,
                )
                page_errors.append({"label": r.label, "action": r.action, "message": r.error_message})

        reset_result = await self.form_tester.resetDropdownState()
        console.print_form_group_result(
            reset_result.success, "DROPDOWN_RESET", reset_result.group_name, reset_result.label, reset_result.action
        )
        if reset_result.success:
            page_passed += 1
            self.metadata_logger.log_action(
                url=url, action=reset_result.action,
                element_label=reset_result.label, element_type="DROPDOWN"
            )
        else:
            page_failed += 1
            ss_path = await self.screenshot_manager.capture_error(
                page=self.page, url=url,
                action=reset_result.action, error_type="dropdown_reset_failure"
            )
            self.metadata_logger.log_error(
                url=url, action=reset_result.action,
                error_type="dropdown_reset_failure",
                error_message=reset_result.error_message,
                element_label=reset_result.label, element_type="DROPDOWN",
                screenshot_path=ss_path,
            )
            page_errors.append({
                "label": reset_result.label,
                "action": reset_result.action,
                "message": reset_result.error_message
            })
        # ===== DROPDOWN TEST END =====

        # ── INTERACTOR LOOP with section grouping ──────────────────────────────
        # Index-based while loop with processed-set deduplication.
        # CHECKBOX and RADIO are skipped here — already handled by form_tester above.
        #
        # DEDUP GUARANTEE: Every element is identified by its structural fingerprint.
        # The `processed` set is NEVER cleared between drift-recovery cycles —
        # re-discovered elements are filtered through it, so no element is ever
        # interacted with more than once per page test.
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

            # START: Sidebar Navigation Processing
            # Skip NAV_ITEM — handled sequentially after this loop
            if elem.element_type in _SIDEBAR_HANDLED:
                idx += 1
                continue
            # END: Sidebar Navigation Processing

            # Skip ACCORDION — handled after main loop by _run_accordion_testing()
            if elem.element_type in _ACCORDION_HANDLED:
                idx += 1
                continue

            # Build dedup key using structural fingerprint so it survives
            # page drift + back-navigation re-discovery cycles.
            # Falls back to (selector, label, type) for elements without fingerprints.
            elem_key = elem.fingerprint if elem.fingerprint else (
                elem.selector, elem.label, elem.element_type.name
            )

            if elem_key in processed:
                idx += 1
                continue

            # ── GLOBAL dedup: skip elements already tested on a PREVIOUS page ──
            if config.GLOBAL_ELEMENT_DEDUP and self.state_tracker.is_element_tested(elem_key):
                logger.debug(
                    "Skipping already tested element: [%s] %s",
                    elem.element_type.name, elem.label,
                )
                console.print_action(
                    True, elem.element_type.name, elem.label,
                    "skipped_already_tested",
                )
                page_skipped += 1
                processed.add(elem_key)   # keep local set coherent for drift-recovery filter
                idx += 1
                continue
            # ── END global dedup ──────────────────────────────────────────────

            # Re-check for page drift from a PREVIOUS interaction
            current_url = self.page.url
            if self._normalize(current_url) != self._normalize(canonical_url):
                logger.debug("Page drifted to %s, recovering to %s", current_url, canonical_url)
                success = await self._safe_navigate(canonical_url)
                if not success:
                    break
                # Re-discover fresh handles BUT keep processed set intact.
                # Filter out already-tested elements so we never re-test.
                new_elements = await finder.discover()
                # ===== OPTIMIZATION START =====
                # Element filter extracted to _filter_elements() — was copy-pasted 3×
                element_list = self._filter_elements(new_elements, processed)
                # ===== OPTIMIZATION END =====
                current_section = ""   # reset section header only
                idx = 0
                continue

            # Print section header when the section changes
            section = _SECTION_LABELS.get(elem.element_type, "OTHER")
            if section != current_section:
                console.print_section_header(section)
                current_section = section

            result = await self.interactor.interact(elem)

            processed.add(elem_key)
            # Mark globally tested (crawl-wide) regardless of outcome
            self.state_tracker.mark_element_tested(elem_key)
            self.state_tracker.mark_interaction_tested(elem_key, elem.element_type.name)
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

                # Drift check AFTER success — same safe re-discovery logic
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    logger.debug("Post-interact drift detected, recovering to %s", canonical_url)
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    new_elements = await finder.discover()
                    # ===== OPTIMIZATION START =====
                    element_list = self._filter_elements(new_elements, processed)
                    # ===== OPTIMIZATION END =====
                    current_section = ""
                    idx = 0
                else:
                    # ── Modal detection: after a successful BUTTON click, check
                    # whether a modal/dialog/drawer just opened and test it.
                    if elem.element_type == ElementType.BUTTON:
                        modal_stats = await self._detect_and_test_modal(
                            canonical_url=canonical_url,
                            depth=0,
                            processed=processed,
                        )
                        page_passed  += modal_stats[0]
                        page_failed  += modal_stats[1]
                        page_skipped += modal_stats[2]

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

                # Navigate back if drifted after error — same safe re-discovery
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    success = await self._safe_navigate(canonical_url)
                    if not success:
                        break
                    new_elements = await finder.discover()
                    # ===== OPTIMIZATION START =====
                    element_list = self._filter_elements(new_elements, processed)
                    # ===== OPTIMIZATION END =====
                    current_section = ""
                    idx = 0

        # ── Per-page error block ───────────────────────────────────────────────
        if page_errors:
            console.print_error_block(page_errors)

        # START: Intelligent Recursive Functional Testing
        # Run sidebar navigation AFTER all other element testing on this page.
        # When a sidebar click opens a NEW unvisited page, recursively call
        # _test_page() to fully test it before continuing to the next sidebar item.
        # self.visited prevents re-testing any page regardless of recursion depth.
        nav_elements = [e for e in elements if e.element_type in _SIDEBAR_HANDLED]
        if nav_elements:
            logger.info("[Recursive] Current page fully tested: %s", url)
            logger.info("[Recursive] Sidebar detected. Creating sidebar inventory (%d items).",
                        len(nav_elements))
            sb_passed, sb_failed, sb_skipped = await self._run_sidebar_navigation(
                url=url,
                canonical_url=canonical_url,
                nav_elements=nav_elements,
                processed=processed,
                finder=finder,
            )
            page_passed  += sb_passed
            page_failed  += sb_failed
            page_skipped += sb_skipped
        else:
            logger.info("[Recursive] Current page fully tested: %s", url)
        # END: Intelligent Recursive Functional Testing

        # ===== ACCORDION TESTING START =====
        # Test accordion/collapsible elements LAST — they expand hidden content,
        # which could otherwise interfere with form-field testing above.
        accordion_elements = [e for e in elements if e.element_type in _ACCORDION_HANDLED]
        if accordion_elements:
            logger.info("[Accordion] %d accordion element(s) found. Running expand tests.",
                        len(accordion_elements))
            console.print_section_header("ACCORDIONS")
            ac_passed, ac_failed, ac_skipped = await self._run_accordion_testing(
                url=url,
                canonical_url=canonical_url,
                accordion_elements=accordion_elements,
                processed=processed,
            )
            page_passed  += ac_passed
            page_failed  += ac_failed
            page_skipped += ac_skipped
        # ===== ACCORDION TESTING END =====

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

    # ===== OPTIMIZATION START =====
    def _filter_elements(self, elements: list, processed: set) -> list:
        """
        Filter a freshly-discovered element list to only unprocessed, interactor-
        loop-eligible elements.

        Used after every drift-recovery re-discover() call.  Was previously an
        identical 7-line list-comprehension copy-pasted 3 times in _test_page.
        Extracting here ensures any future change is made in ONE place.
        """
        return [
            e for e in elements
            if (e.fingerprint if e.fingerprint else (e.selector, e.label, e.element_type.name))
            not in processed
            and e.element_type not in _FORM_TESTER_HANDLED
            and e.element_type not in _SIDEBAR_HANDLED
            and e.element_type not in _ACCORDION_HANDLED
        ]
    # ===== OPTIMIZATION END =====

    # START: Sidebar Navigation Processing
    async def _run_sidebar_navigation(
        self,
        url: str,
        canonical_url: str,
        nav_elements: list,
        processed: set,
        finder,
    ) -> tuple:
        """
        Sequentially click each sidebar/navigation element EXACTLY ONCE.

        Algorithm:
          1. Pre-filter against the shared processed set (fingerprint-based).
          2. For each unprocessed element (in discovery order):
               a. Ensure we are on canonical_url before clicking.
               b. Interact via self.interactor.interact().
               c. Mark fingerprint as processed immediately (even on failure).
               d. Check for drift → navigate back to canonical_url.
               e. Log item N/M with label for traceability.
          3. Never restart the loop; never click the same element twice.
          4. Return (passed, failed, skipped) deltas.

        Sidebar elements are processed AFTER the main interactor loop to
        prevent nav drift from disrupting form/button/input testing.
        """
        passed = failed = skipped = 0

        # Only unprocessed sidebar elements:
        #   - fingerprint not yet in this page's processed set, AND
        #   - fingerprint not yet in the crawl-wide nav fingerprint set
        #     (same navbar appears on every page — test it ONCE globally)
        pending = [
            e for e in nav_elements
            if (
                (e.fingerprint if e.fingerprint else (e.selector, e.label, e.element_type.name))
                not in processed
            ) and (
                (e.fingerprint if e.fingerprint else (e.selector, e.label, e.element_type.name))
                not in self._global_nav_fingerprints
            )
        ]

        total = len(pending)
        if total == 0:
            logger.info(
                "[Sidebar] All %d sidebar elements already processed (page or global) — skipping pass",
                len(nav_elements)
            )
            return (0, 0, 0)

        logger.info("[Sidebar] Total sidebar elements found: %d", total)
        console.print_section_header("SIDEBAR_NAV")

        for item_idx, elem in enumerate(pending, start=1):
            elem_key = elem.fingerprint if elem.fingerprint else (
                elem.selector, elem.label, elem.element_type.name
            )

            # Guard: skip if processed by a concurrent/earlier cycle
            if elem_key in processed:
                logger.info("[Sidebar] Skipping already processed sidebar item: %s", elem.label)
                skipped += 1
                continue

            # Ensure we are on the canonical page before each sidebar click
            if self._normalize(self.page.url) != self._normalize(canonical_url):
                logger.debug("[Sidebar] Pre-click drift detected, recovering to %s", canonical_url)
                ok = await self._safe_navigate(canonical_url)
                if not ok:
                    logger.warning("[Sidebar] Could not return to dashboard — aborting sidebar pass")
                    break

            label_str = elem.label or elem.attrs.get("aria-label", "") or elem.selector
            logger.info("[Sidebar] Clicking sidebar item %d/%d: %s", item_idx, total, label_str)

            try:
                result = await self.interactor.interact(elem)
            except Exception as exc:
                logger.error("[Sidebar] Sidebar navigation failed safely on '%s': %s", label_str, exc)
                processed.add(elem_key)
                failed += 1
                # Always return to canonical after an exception
                await self._safe_navigate(canonical_url)
                continue

            # Mark processed immediately — regardless of outcome
            # Also add to the crawl-wide nav set and StateTracker to prevent
            # re-testing on other pages.
            processed.add(elem_key)
            self._global_nav_fingerprints.add(elem_key)
            self.state_tracker.mark_element_tested(elem_key)
            self.state_tracker.mark_interaction_tested(elem_key, elem.element_type.name)

            if result.action_performed.startswith("skipped_stale"):
                logger.debug("[Sidebar] Stale sidebar element skipped: %s", label_str)
                console.print_action(True, result.element_type, label_str, result.action_performed)
                self.metadata_logger.log_action(
                    url=url, action=result.action_performed,
                    element_label=label_str, element_type=result.element_type,
                )
                skipped += 1

            elif result.success:
                logger.info("[Sidebar] Sidebar element processed successfully: %s", label_str)
                console.print_action(True, result.element_type, label_str, result.action_performed)
                self.metadata_logger.log_action(
                    url=url, action=result.action_performed,
                    element_label=label_str, element_type=result.element_type,
                )
                passed += 1

            else:
                logger.error("[Sidebar] Sidebar navigation failed safely on '%s': %s",
                             label_str, result.error_message[:120])
                ss_path = await self.screenshot_manager.capture_error(
                    page=self.page, url=url,
                    action=result.action_performed, error_type="sidebar_interaction_error",
                )
                self.metadata_logger.log_error(
                    url=url, action=result.action_performed,
                    error_type="sidebar_interaction_error",
                    error_message=result.error_message,
                    element_label=label_str, element_type=result.element_type,
                    screenshot_path=ss_path,
                )
                console.print_action(False, result.element_type, label_str, "ERROR captured")
                failed += 1

            # START: Intelligent Recursive Functional Testing
            # After each sidebar click, check if a new page was opened.
            # If the destination is unvisited and same-domain: fully test it
            # recursively BEFORE returning to the canonical dashboard URL.
            # self.visited is the global BFS dedup set — adding here prevents
            # the BFS loop from re-testing the same page later.
            current_url = self.page.url
            if self._normalize(current_url) != self._normalize(canonical_url):
                norm_current = self._normalize(current_url)
                if (norm_current
                        and norm_current not in self.visited
                        and self._is_same_domain(current_url)):
                    logger.info("[Recursive] New page detected after sidebar item %d/%d: %s",
                                item_idx, total, current_url)
                    logger.info("[Recursive] Starting recursive testing of: %s", current_url)
                    self.visited.add(norm_current)
                    await self._test_page(current_url)
                    logger.info("[Recursive] Recursive testing complete. Returning to parent page.")
                else:
                    logger.info("[Recursive] Skipping already tested page: %s", current_url)

                logger.info("[Sidebar] Returning to dashboard after sidebar item %d/%d",
                            item_idx, total)
                ok = await self._safe_navigate(canonical_url)
                if not ok:
                    logger.warning("[Sidebar] Could not return to dashboard — aborting sidebar pass")
                    break
            # END: Intelligent Recursive Functional Testing

        logger.info(
            "[Sidebar] Sidebar pass complete: %d passed, %d failed, %d skipped",
            passed, failed, skipped,
        )
        return (passed, failed, skipped)
    # END: Sidebar Navigation Processing

    async def _safe_navigate(self, url: str) -> bool:
        """Navigate with error handling. Returns True on success."""
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT)
            self._last_nav_response = response
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

    async def _detect_and_test_modal(self, canonical_url: str, depth: int, processed: set) -> tuple[int, int, int]:
        """Detect an open modal/dialog and test its interactive elements.

        This method is deliberately conservative:
        - Bounded by `config.MAX_MODAL_DEPTH` to avoid infinite recursion.
        - Only interacts with newly discovered elements that are not in `processed`.
        - Stops when the modal is no longer present or when navigation occurs.
        Returns a (passed, failed, skipped) tuple.
        """
        passed = failed = skipped = 0

        if depth >= config.MAX_MODAL_DEPTH:
            logger.debug("Max modal depth reached (%d). Skipping modal tests.", config.MAX_MODAL_DEPTH)
            return (0, 0, 0)

        # ── Global modal state dedup ────────────────────────────────────────────
        modal_state_key = self.state_tracker.build_state_key(
            canonical_url, "modal", str(depth)
        )
        if self.state_tracker.is_state_tested(modal_state_key):
            logger.debug(
                "Skipping duplicate interaction: modal at '%s' depth=%d already tested",
                canonical_url, depth,
            )
            return (0, 0, 0)
        self.state_tracker.mark_state_tested(modal_state_key)
        # ── END modal state dedup ───────────────────────────────────────────────

        try:
            modal_present = await self.page.evaluate(
                """
                () => !!Array.from(document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]'))
                """
            )
        except Exception:
            modal_present = False

        if not modal_present:
            return (0, 0, 0)

        logger.info("[Modal] Modal detected — running bounded modal element tests (depth=%d)", depth)

        finder = ElementFinder(self.page)
        try:
            elements = await finder.discover()
        except Exception as exc:
            logger.debug("[Modal] Element discovery failed: %s", exc)
            return (0, 0, 0)

        # Filter to elements not yet processed and limit to a safe count
        modal_elements = []
        for e in elements:
            key = e.fingerprint if e.fingerprint else (e.selector, e.label, e.element_type.name)
            if key in processed:
                continue
            modal_elements.append(e)
            if len(modal_elements) >= 20:
                break

        # Interact sequentially with modal elements (safe, non-destructive)
        for elem in modal_elements:
            key = elem.fingerprint if elem.fingerprint else (elem.selector, elem.label, elem.element_type.name)
            try:
                result = await self.interactor.interact(elem)
            except Exception as exc:
                logger.error("[Modal] Interaction exception on modal element %s: %s", elem.label, exc)
                processed.add(key)
                failed += 1
                # If navigation occurred, stop testing
                if self._normalize(self.page.url) != self._normalize(canonical_url):
                    break
                continue

            processed.add(key)

            if result.action_performed.startswith("skipped_stale"):
                skipped += 1
                self.metadata_logger.log_action(
                    url=canonical_url, action=result.action_performed,
                    element_label=result.element_label, element_type=result.element_type,
                )
                continue

            if result.success:
                passed += 1
                self.metadata_logger.log_action(
                    url=canonical_url, action=result.action_performed,
                    element_label=result.element_label, element_type=result.element_type,
                )
            else:
                failed += 1
                ss = await self.screenshot_manager.capture_error(
                    page=self.page, url=canonical_url, action=result.action_performed, error_type="modal_interaction_error"
                )
                self.metadata_logger.log_error(
                    url=canonical_url, action=result.action_performed,
                    error_type="modal_interaction_error",
                    error_message=result.error_message, element_label=result.element_label,
                    element_type=result.element_type, screenshot_path=ss,
                )

            # If the modal closed or navigation occurred, stop testing further modal elements
            try:
                still_modal = await self.page.evaluate(
                    """
                    () => !!Array.from(document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]'))
                    """
                )
            except Exception:
                still_modal = False

            if not still_modal or self._normalize(self.page.url) != self._normalize(canonical_url):
                # If a nested modal opened, recurse one level deeper to test it
                if self._normalize(self.page.url) == self._normalize(canonical_url):
                    logger.debug("[Modal] Modal closed after interaction; ending modal pass.")
                else:
                    logger.debug("[Modal] Navigation detected during modal testing: %s", self.page.url)
                break

        # After interacting, if still in the same canonical page and modal still present,
        # attempt one recursive pass (depth+1) to handle nested modals safely.
        try:
            still_modal = await self.page.evaluate(
                """
                () => !!Array.from(document.querySelectorAll('[role="dialog"], .modal, [aria-modal="true"]'))
                """
            )
        except Exception:
            still_modal = False

        if still_modal and self._normalize(self.page.url) == self._normalize(canonical_url):
            more = await self._detect_and_test_modal(canonical_url=canonical_url, depth=depth + 1, processed=processed)
            passed += more[0]
            failed += more[1]
            skipped += more[2]

        return (passed, failed, skipped)
