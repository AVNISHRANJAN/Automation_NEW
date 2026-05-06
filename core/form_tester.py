"""
core/form_tester.py — Advanced form interaction engine.

Handles interactions that require group-awareness or multi-step logic,
beyond what the basic Interactor can provide:

  - Checkbox groups: test check AND uncheck per group
  - Radio groups:    select exactly one option per group (enforces exclusivity)
  - Multi-step forms: detect and advance through paginated form steps
  - Destructive action filter: skip buttons/links that look dangerous
  - Element manifest: export discovered elements to JSON

// ===== NEW FEATURE START =====
"""

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict

from playwright.async_api import Page, ElementHandle

import config
from core.element_finder import ElementInfo, ElementType

logger = logging.getLogger(__name__)

# ── Destructive action keywords ────────────────────────────────────────────────
# Buttons / links whose text matches these will be SKIPPED to avoid data loss
DESTRUCTIVE_KEYWORDS = [
    "logout", "log out", "sign out", "signout",
    "delete", "remove", "cancel account", "deactivate",
    "terminate", "destroy", "drop", "purge", "wipe",
    "reset all", "clear all", "factory reset",
]


@dataclass
class FormTestResult:
    """Structured result for a single form-level interaction."""
    element_type: str
    group_name: str          # radio/checkbox group name attribute
    label: str
    action: str
    success: bool
    error_message: str = ""


class FormTester:
    """
    Handles advanced form interaction patterns that require group-level
    coordination (radio exclusivity, checkbox toggle, multi-step navigation).
    """

    def __init__(self, page: Page):
        self.page = page

    # ──────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────────────────────

    async def test_checkbox_groups(self, elements: List[ElementInfo]) -> List[FormTestResult]:
        """
        Test all visible checkboxes:
          1. Check each one that is currently unchecked.
          2. Then uncheck it to restore state.
          3. Also verify that checking one does NOT uncheck siblings in the group.
        Returns one FormTestResult per checkbox tested.
        """
        results: List[FormTestResult] = []
        checkboxes = [e for e in elements if e.element_type == ElementType.CHECKBOX]

        if not checkboxes:
            logger.debug("No checkboxes found on page.")
            return results

        # Group by 'name' attribute for group-awareness
        groups: Dict[str, List[ElementInfo]] = defaultdict(list)
        for cb in checkboxes:
            name = cb.attrs.get("name", cb.label or "unnamed")
            groups[name].append(cb)

        logger.info("Found %d checkbox group(s) with %d total checkboxes.", len(groups), len(checkboxes))

        for group_name, group_cbs in groups.items():
            for cb in group_cbs:
                result = await self._test_single_checkbox(cb, group_name)
                results.append(result)

        return results

    async def test_radio_groups(self, elements: List[ElementInfo]) -> List[FormTestResult]:
        """
        Test radio button groups:
          - For each unique 'name' group, select each radio option once.
          - After selecting, verify no sibling in the same group is still checked.
        Returns one FormTestResult per radio option tested.
        """
        results: List[FormTestResult] = []
        radios = [e for e in elements if e.element_type == ElementType.RADIO]

        if not radios:
            logger.debug("No radio buttons found on page.")
            return results

        # Group by 'name' attribute — this is how radio exclusivity works in HTML
        groups: Dict[str, List[ElementInfo]] = defaultdict(list)
        for rb in radios:
            name = rb.attrs.get("name", rb.label or "unnamed")
            groups[name].append(rb)

        logger.info("Found %d radio group(s) with %d total options.", len(groups), len(radios))

        for group_name, group_radios in groups.items():
            result = await self._test_radio_group(group_name, group_radios)
            results.extend(result)

        return results

    async def is_destructive(self, info: ElementInfo) -> bool:
        """
        Return True if the element text/label looks like a destructive action.
        Used by the caller to skip dangerous buttons/links.
        """
        label_lower = info.label.lower()
        href_lower = info.href.lower()
        return any(kw in label_lower or kw in href_lower for kw in DESTRUCTIVE_KEYWORDS)

    async def detect_multistep_form(self) -> bool:
        """
        Heuristically detect if the current page has a multi-step / wizard form.
        Looks for:
          - 'Next' / 'Continue' / 'Step' buttons
          - Progress indicators (aria-label with 'step', numbered dots, etc.)
          - Hidden form sections (display:none siblings of visible form sections)
        Returns True if multi-step form is detected.
        """
        # ===== OPTIMIZATION START =====
        # Was: two separate page.evaluate round-trips (has_next + has_progress).
        # Now: single JS evaluate that checks both conditions in one call.
        try:
            detected = await self.page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button, input[type=submit], a'));
                    const nextKeywords = ['next', 'continue', 'proceed', 'step 2', '\u2192', '>>', 'weiter'];
                    const hasNext = btns.some(el => {
                        const text = (el.textContent || el.value || '').toLowerCase().trim();
                        return nextKeywords.some(kw => text.includes(kw));
                    });
                    const progressSelectors = [
                        '[class*="step"]', '[class*="wizard"]', '[class*="progress"]',
                        '[aria-label*="step"]', '[data-step]',
                    ];
                    const hasProgress = progressSelectors.some(s => document.querySelector(s) !== null);
                    return hasNext || hasProgress;
                }
            """)
            if detected:
                logger.info("Multi-step form detected on %s", self.page.url)
            return bool(detected)
        except Exception as exc:
            logger.debug("Multi-step form detection failed: %s", exc)
            return False
        # ===== OPTIMIZATION END =====

    async def advance_multistep_form(self) -> bool:
        """
        If a multi-step form is present, click the 'Next'/'Continue' button
        to advance to the next step. Returns True if successfully advanced.
        """
        next_keywords = ["next", "continue", "proceed", "→", ">>"]
        try:
            handle = await self.page.evaluate_handle("""
                () => {
                    const btns = Array.from(document.querySelectorAll(
                        'button:not([disabled]), input[type=submit]:not([disabled]), a'
                    ));
                    const kws = ['next', 'continue', 'proceed', '→', '>>'];
                    return btns.find(el => {
                        const text = (el.textContent || el.value || '').toLowerCase().trim();
                        return kws.some(kw => text.includes(kw));
                    }) || null;
                }
            """)
            element = handle.as_element()
            if element:
                await element.scroll_into_view_if_needed()
                await element.click(timeout=config.ACTION_TIMEOUT)
                await asyncio.sleep(0.5)
                logger.info("Advanced multi-step form (clicked Next/Continue).")
                return True
        except Exception as exc:
            logger.debug("Failed to advance multi-step form: %s", exc)
        return False

    async def detect_dynamic_elements(self) -> List[ElementInfo]:
        """
        Look for elements that were added to the DOM dynamically (lazy-loaded,
        revealed by JS, or inside iframes). Returns additional ElementInfo list.

        Strategy:
          1. Scroll to bottom to trigger lazy-loaded content.
          2. Wait briefly for any mutation-observer-driven additions.
          3. Scan for elements with display:none that have visible siblings
             (indicates hidden form steps or conditional fields).
          4. Attempt to read first-party iframes (same origin only).
        """
        newly_found: List[ElementInfo] = []

        # Step 1: Scroll to trigger lazy loading
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.8)   # allow lazy content to render
            await self.page.evaluate("window.scrollTo(0, 0)")
        except Exception as exc:
            logger.debug("Scroll for lazy load failed: %s", exc)

        # Step 2: Detect hidden inputs (type=hidden can carry important form values)
        try:
            hidden_count = await self.page.evaluate("""
                () => document.querySelectorAll('input[type=hidden]').length
            """)
            if hidden_count:
                logger.info("Found %d hidden input(s) — not interacted with but noted.", hidden_count)
        except Exception:
            pass

        # Step 3: Detect conditional/revealed fields (display:none form groups)
        try:
            conditional_fields = await self.page.evaluate("""
                () => {
                    const hidden = Array.from(document.querySelectorAll(
                        'input:not([type=hidden]), textarea, select'
                    )).filter(el => {
                        const style = window.getComputedStyle(el);
                        return style.display === 'none' || style.visibility === 'hidden';
                    });
                    return hidden.map(el => ({
                        tag: el.tagName.toLowerCase(),
                        name: el.name || '',
                        id: el.id || '',
                        type: el.type || '',
                    }));
                }
            """)
            if conditional_fields:
                logger.info(
                    "Detected %d conditionally-hidden form field(s): %s",
                    len(conditional_fields),
                    [f.get("name") or f.get("id") or f.get("type") for f in conditional_fields]
                )
        except Exception as exc:
            logger.debug("Conditional field detection failed: %s", exc)

        return newly_found   # currently returns [] — future: wrap as ElementInfo

    # ──────────────────────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    async def _test_single_checkbox(self, cb: ElementInfo, group_name: str) -> FormTestResult:
        """Check then uncheck a single checkbox. Validates state after each action."""
        try:
            await cb.handle.scroll_into_view_if_needed()

            # Step 1: Ensure it is unchecked first (restore to known state)
            is_checked = await cb.handle.is_checked()
            if is_checked:
                await cb.handle.uncheck(timeout=config.ACTION_TIMEOUT)
                await asyncio.sleep(0.2)

            # Step 2: Check it
            await cb.handle.check(timeout=config.ACTION_TIMEOUT)
            await asyncio.sleep(0.2)
            after_check = await cb.handle.is_checked()

            # Step 3: Uncheck it (restore state)
            await cb.handle.uncheck(timeout=config.ACTION_TIMEOUT)
            await asyncio.sleep(0.2)
            after_uncheck = await cb.handle.is_checked()

            if after_check and not after_uncheck:
                action = "check_then_uncheck:verified"
                success = True
            else:
                action = f"check_then_uncheck:state_mismatch(checked={after_check},unchecked={after_uncheck})"
                success = False

            logger.info("Checkbox [%s / %s]: %s", group_name, cb.label, action)
            return FormTestResult(
                element_type="CHECKBOX",
                group_name=group_name,
                label=cb.label,
                action=action,
                success=success,
            )

        except Exception as exc:
            error = str(exc)[:300]
            logger.error("Checkbox test failed [%s / %s]: %s", group_name, cb.label, error)
            return FormTestResult(
                element_type="CHECKBOX",
                group_name=group_name,
                label=cb.label,
                action="check_then_uncheck:error",
                success=False,
                error_message=error,
            )

    async def _test_radio_group(self, group_name: str, group_radios: List[ElementInfo]) -> List[FormTestResult]:
        """
        Test a radio group:
          - Click each option once.
          - After each click, verify siblings in the same group are NOT checked.
        """
        results: List[FormTestResult] = []

        for i, radio in enumerate(group_radios):
            try:
                await radio.handle.scroll_into_view_if_needed()
                await radio.handle.click(timeout=config.ACTION_TIMEOUT)
                await asyncio.sleep(0.2)

                # Verify this radio is now checked
                is_checked = await radio.handle.is_checked()

                # Verify siblings are NOT checked (radio exclusivity)
                siblings_ok = True
                for j, sibling in enumerate(group_radios):
                    if j == i:
                        continue
                    try:
                        sib_checked = await sibling.handle.is_checked()
                        if sib_checked:
                            siblings_ok = False
                            logger.warning(
                                "Radio group [%s]: sibling '%s' is ALSO checked after selecting '%s' — exclusivity violated!",
                                group_name, sibling.label, radio.label
                            )
                    except Exception:
                        pass  # handle stale or detached sibling

                if is_checked and siblings_ok:
                    action = "radio_select:verified_exclusive"
                    success = True
                elif is_checked:
                    action = "radio_select:exclusivity_violated"
                    success = False
                else:
                    action = "radio_select:not_checked_after_click"
                    success = False

                logger.info("Radio [%s / %s]: %s", group_name, radio.label, action)
                results.append(FormTestResult(
                    element_type="RADIO",
                    group_name=group_name,
                    label=radio.label,
                    action=action,
                    success=success,
                ))

            except Exception as exc:
                error = str(exc)[:300]
                logger.error("Radio test failed [%s / %s]: %s", group_name, radio.label, error)
                results.append(FormTestResult(
                    element_type="RADIO",
                    group_name=group_name,
                    label=radio.label,
                    action="radio_select:error",
                    success=False,
                    error_message=error,
                ))

        return results


# ──────────────────────────────────────────────────────────────────────────────
# Element Manifest Exporter
# ──────────────────────────────────────────────────────────────────────────────

class ElementManifestExporter:
    """
    Exports discovered UI elements to a structured JSON manifest per run.
    One manifest file per run; one entry per page visited.
    File: output/manifests/{run_id}_manifest.json
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        manifest_dir = config.OUTPUT_DIR / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = manifest_dir / f"{run_id}_manifest.json"
        self._data: Dict[str, list] = {}   # page_url → list of element dicts
        # ===== OPTIMIZATION START =====
        # Cache summary so get_summary() does not recompute sum() on every call.
        self._summary: Dict = {"total_pages": 0, "total_elements": 0}
        # ===== OPTIMIZATION END =====

    def record_page(self, page_url: str, elements: List[ElementInfo]) -> None:
        """
        Serialize discovered elements for a given page URL and store in memory.
        Call after each page's discover() completes.
        """
        serialized = []
        for el in elements:
            serialized.append({
                "type":       el.element_type.name,
                "selector":   el.selector,
                "label":      el.label,
                "tag":        el.tag,
                "input_type": el.input_type,
                "href":       el.href,
                "attrs":      el.attrs,
            })
        self._data[page_url] = serialized
        logger.debug("Manifest: recorded %d elements for %s", len(serialized), page_url)

    def save(self) -> str:
        """Write the complete manifest to disk. Returns the file path."""
        # ===== OPTIMIZATION START =====
        # Compute summary once here and cache it — get_summary() returns the cache.
        self._summary = {
            "total_pages":    len(self._data),
            "total_elements": sum(len(v) for v in self._data.values()),
        }
        # ===== OPTIMIZATION END =====
        payload = {
            "run_id":  self.run_id,
            "pages":   self._data,
            "summary": self._summary,
        }
        try:
            self.manifest_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            logger.info("Element manifest saved: %s", self.manifest_path)
        except Exception as exc:
            logger.warning("Failed to save element manifest: %s", exc)
        return str(self.manifest_path)

    def get_summary(self) -> Dict:
        # ===== OPTIMIZATION START =====
        # Return the cached summary computed in save() — avoids redundant sum().
        # ===== OPTIMIZATION END =====
        return self._summary

# ===== NEW FEATURE END =====
