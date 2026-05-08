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

    def __init__(self, page: Page, state_tracker=None):
        self.page = page
        # Optional crawl-wide state tracker — enables global dedup for form groups.
        # Falls back gracefully to per-call dedup when not provided.
        self._state_tracker = state_tracker

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
            # ── Global dedup: skip checkbox groups already tested crawl-wide ──────────────
            if self._state_tracker is not None:
                ck_state_key = self._state_tracker.build_state_key(
                    self.page.url, "checkbox_group", group_name
                )
                if self._state_tracker.is_state_tested(ck_state_key):
                    logger.debug(
                        "Skipping already tested element: checkbox group '%s'", group_name
                    )
                    continue
                self._state_tracker.mark_state_tested(ck_state_key)
            # ── END global dedup ──────────────────────────────────────────────────
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
            # ── Global dedup: skip radio groups already tested crawl-wide ───────────────
            if self._state_tracker is not None:
                rb_state_key = self._state_tracker.build_state_key(
                    self.page.url, "radio_group", group_name
                )
                if self._state_tracker.is_state_tested(rb_state_key):
                    logger.debug(
                        "Skipping already tested element: radio group '%s'", group_name
                    )
                    continue
                self._state_tracker.mark_state_tested(rb_state_key)
            # ── END global dedup ──────────────────────────────────────────────────
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

    # ===== DROPDOWN TEST START =====
    async def detectDropdowns(self) -> List[dict]:
        """
        Detect native and custom dropdowns (including searchable variants).
        Returns a lightweight descriptor list for dependency-chain testing.
        """
        try:
            items = await self.page.evaluate("""
                () => {
                    const isVisible = (el) => {
                        const cs = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return cs.display !== 'none' && cs.visibility !== 'hidden'
                            && r.width > 0 && r.height > 0;
                    };
                    const cssPath = (el) => {
                        const parts = [];
                        let node = el;
                        let guard = 0;
                        while (node && node.nodeType === 1 && guard < 8) {
                            let seg = node.tagName.toLowerCase();
                            if (node.id) {
                                seg += `#${node.id}`;
                                parts.unshift(seg);
                                break;
                            }
                            const sibs = node.parentElement
                                ? Array.from(node.parentElement.children).filter(s => s.tagName === node.tagName)
                                : [];
                            if (sibs.length > 1) {
                                seg += `:nth-of-type(${sibs.indexOf(node) + 1})`;
                            }
                            parts.unshift(seg);
                            node = node.parentElement;
                            guard += 1;
                        }
                        return parts.join(' > ');
                    };
                    const isPaginationLikeSelect = (el) => {
                        if (!el || el.tagName.toLowerCase() !== 'select') return false;
                        const values = Array.from(el.options || []).map(o => (o.value || '').trim());
                        const txts = Array.from(el.options || []).map(o => (o.textContent || '').trim());
                        const smallSet = values.length > 0 && values.length <= 6;
                        const looksPageSize = values.every(v => /^\\d+$/.test(v)) && txts.every(t => /^\\d+$/.test(t));
                        const hasCommonSizes = values.some(v => ['10', '25', '50', '100'].includes(v));
                        const cls = (el.className || '').toLowerCase();
                        const nearPaginationText = !!el.closest('[class*="paginate"], [class*="pagination"], [id*="paginate"], [id*="pagination"]');
                        return smallSet && looksPageSize && hasCommonSizes && (nearPaginationText || cls.includes('w-17') || cls.includes('text-sm'));
                    };
                    const keyFor = (el) => {
                        if (el.id) return `#${el.id}`;
                        const dataTest = el.getAttribute('data-testid') || el.getAttribute('data-test-id');
                        if (dataTest) return `[data-testid="${dataTest}"]`;
                        const name = el.getAttribute('name');
                        if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
                        return cssPath(el);
                    };
                    const out = [];
                    const pushUnique = (obj) => {
                        if (!out.some(x => x.key === obj.key)) out.push(obj);
                    };

                    document.querySelectorAll('select').forEach((el) => {
                        if (!isVisible(el)) return;
                        if (isPaginationLikeSelect(el)) return;
                        pushUnique({
                            key: keyFor(el),
                            kind: 'native',
                            searchable: false,
                            disabled: !!el.disabled,
                            optionsCount: el.options ? el.options.length : 0,
                            label: el.getAttribute('aria-label') || el.name || el.id || 'select'
                        });
                    });

                    const customSelectors = [
                        '.select2-selection',
                        '.ant-select-selector',
                        '.MuiSelect-select',
                        '.MuiAutocomplete-inputRoot',
                        '.dropdown-toggle',
                        '[role="combobox"]',
                        '[aria-haspopup="listbox"]',
                        '[class*="select"][class*="control"]',
                        '[class*="dropdown"][class*="control"]',
                    ];
                    document.querySelectorAll(customSelectors.join(',')).forEach((el) => {
                        if (!isVisible(el)) return;
                        const host = el.closest('[data-testid], [id], [name], .ant-select, .select2, .MuiFormControl-root, .dropdown') || el;
                        const searchable = !!host.querySelector('input[type="search"], input[role="combobox"], input[type="text"]');
                        const disabled = host.matches('[aria-disabled="true"], .ant-select-disabled, .Mui-disabled, .select2-container--disabled')
                            || !!host.querySelector('[disabled]');
                        pushUnique({
                            key: keyFor(host),
                            kind: 'custom',
                            searchable,
                            disabled,
                            optionsCount: 0,
                            label: host.getAttribute('aria-label') || host.getAttribute('name') || host.id || host.className || 'custom-dropdown'
                        });
                    });
                    return out;
                }
            """)
            return items or []
        except Exception as exc:
            logger.debug("Dropdown detection failed: %s", exc)
            return []

    async def waitForDropdownUpdate(self, before_state: List[dict], retries: int = 8) -> bool:
        """
        Wait for async dropdown dependency updates (options, enabled-state, spinners).
        """
        spinner_selector = (
            ".loading, .loader, .spinner, .ant-spin-spinning, .MuiCircularProgress-root, "
            "[aria-busy='true'], [data-loading='true']"
        )
        for _ in range(retries):
            try:
                has_spinner = await self.page.locator(spinner_selector).count()
                if has_spinner:
                    await asyncio.sleep(0.4)
                    continue

                changed = await self.page.evaluate("""
                    (prev) => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const cs = window.getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                        };
                        const lookup = (key) => {
                            if (key.startsWith('#')) return document.querySelector(key);
                            const m = key.match(/^([a-z0-9_-]+)\\[data-dd-idx="(\\d+)"\\]$/i);
                            if (m) return document.querySelectorAll(m[1])[Number(m[2])] || null;
                            try { return document.querySelector(key); } catch { return null; }
                        };
                        for (const item of prev || []) {
                            const el = lookup(item.key);
                            if (!isVisible(el)) continue;
                            const disabledNow = !!(el.disabled || el.matches?.('[aria-disabled="true"], .Mui-disabled, .ant-select-disabled'));
                            if (disabledNow !== !!item.disabled) return true;
                            if (item.kind === 'native' && el.options && el.options.length !== (item.optionsCount || 0)) return true;
                        }
                        return false;
                    }
                """, before_state)
                if changed:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False

    async def handleDynamicDropdowns(self) -> List[FormTestResult]:
        """
        Execute dependency-chain dropdown selections with bounded retries.
        """
        results: List[FormTestResult] = []
        tried_keys: set[str] = set()
        max_rounds = 8

        for _ in range(max_rounds):
            dropdowns = await self.detectDropdowns()
            if not dropdowns:
                break

            actionable = [d for d in dropdowns if not d.get("disabled") and d.get("key") not in tried_keys]
            if not actionable:
                break

            progressed = False
            for dd in actionable:
                key = dd.get("key", "")
                label = dd.get("label", key)
                before = dropdowns
                tried_keys.add(key)

                # ── Global dropdown dedup: skip if already tested crawl-wide ─────────────
                if self._state_tracker is not None:
                    dd_state_key = self._state_tracker.build_state_key(
                        self.page.url, "dropdown", key
                    )
                    if self._state_tracker.is_state_tested(dd_state_key):
                        logger.debug(
                            "Skipping duplicate interaction: dropdown '%s' already tested",
                            label,
                        )
                        continue
                    self._state_tracker.mark_state_tested(dd_state_key)
                # ── END global dropdown dedup ────────────────────────────────────
                try:
                    action = await self.page.evaluate("""
                        (item) => {
                            const isPlaceholder = (text, value) => {
                                const t = (text || '').trim().toLowerCase();
                                const v = (value || '').trim().toLowerCase();
                                if (!v) return true;
                                return ['select', 'select...', 'choose', 'choose...', 'please select'].some(k => t === k || t.startsWith(k));
                            };
                            const lookup = (key) => {
                                if (!key) return null;
                                try { return document.querySelector(key); } catch { return null; }
                            };
                            const el = lookup(item.key);
                            if (!el) return 'missing';
                            if (item.kind === 'native' && el.tagName.toLowerCase() === 'select') {
                                const opts = Array.from(el.options || []).filter(o => !o.disabled && !isPlaceholder(o.textContent, o.value));
                                if (!opts.length) return 'no_valid_option';
                                el.value = opts[0].value;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                return `selected:${opts[0].value}`;
                            }
                            el.click();
                            // ===== OPTIMIZATION START =====
                            // Removed duplicate const isPlaceholder declaration that was
                            // shadowing the one above (const re-declaration is a SyntaxError
                            // in strict-mode JS; outer declaration covers this entire scope).
                            // ===== OPTIMIZATION END =====
                            // Small inline retry for portal-rendered option lists.
                            let tries = 5;
                            let valid = null;
                            const optionSelectors = [
                                '[role="option"]:not([aria-disabled="true"])',
                                '.ant-select-item-option:not(.ant-select-item-option-disabled)',
                                '.select2-results__option[aria-disabled!="true"]',
                                '.MuiAutocomplete-option[aria-disabled!="true"]',
                                '.dropdown-menu .dropdown-item:not(.disabled)',
                                'li[role="option"]:not([aria-disabled="true"])'
                            ];
                            while (tries-- > 0 && !valid) {
                                const options = Array.from(document.querySelectorAll(optionSelectors.join(',')));
                                valid = options.find(o => {
                                    const t = (o.textContent || '').trim();
                                    return t && !isPlaceholder(t, t);
                                }) || null;
                                if (!valid) {
                                    const start = Date.now();
                                    while (Date.now() - start < 150) {}
                                }
                            }
                            if (!valid) return 'no_valid_option';
                            valid.click();
                            return `selected:${(valid.textContent || '').trim()}`;
                        }
                    """, dd)

                    await self.waitForDropdownUpdate(before_state=before, retries=8)
                    progressed = True
                    ok = action.startswith("selected:")
                    if not ok and action == "no_valid_option":
                        action = "dropdown_skip_no_valid_option"
                    results.append(FormTestResult(
                        element_type="DROPDOWN",
                        group_name="dynamic_chain",
                        label=label,
                        action=action,
                        success=ok or action == "dropdown_skip_no_valid_option",
                    ))
                except Exception as exc:
                    logger.error("Dropdown dependency failed on page: %s", self.page.url)
                    results.append(FormTestResult(
                        element_type="DROPDOWN",
                        group_name="dynamic_chain",
                        label=label,
                        action="dropdown_select:error",
                        success=False,
                        error_message=str(exc)[:300],
                    ))
            if not progressed:
                break
        return results

    async def testActionButtons(self) -> List[FormTestResult]:
        """
        Test Apply/Search/Submit/Clear style action buttons after dropdown selection.

        Deduplication rules:
          - Buttons are deduplicated by normalised label — only the FIRST occurrence
            of each unique (kind, normalised_text) pair is tested.  Duplicate labels
            (e.g. 12 identical "APPLY" buttons on a careers listing page) are silently
            skipped so they don't generate a flood of false failures.
          - If ALL candidates are disabled we bail out early with a single skip result
            rather than reporting N individual failures.
        """
        results: List[FormTestResult] = []
        try:
            buttons = await self.page.evaluate("""
                () => {
                    const sels = 'button, input[type="button"], input[type="submit"], [role="button"], a';
                    const wanted = ['apply', 'filter', 'search', 'submit', 'clear', 'reset'];
                    return Array.from(document.querySelectorAll(sels))
                        .map((el, idx) => {
                            const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                            const low = txt.toLowerCase();
                            const kind = wanted.find(k => low.includes(k));
                            if (!kind) return null;
                            const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
                            return { idx, text: txt || `button_${idx}`, kind, disabled };
                        })
                        .filter(Boolean);
                }
            """)

            if not buttons:
                return results

            # Deduplicate: keep only the first occurrence of each unique (kind, label)
            seen_dedup: set = set()
            unique_buttons = []
            for b in buttons:
                dedup_key = (b.get("kind", ""), (b.get("text") or "").strip().lower()[:60])
                if dedup_key not in seen_dedup:
                    seen_dedup.add(dedup_key)
                    unique_buttons.append(b)

            # If every unique button is disabled, report once and bail out
            active_buttons = [b for b in unique_buttons if not b.get("disabled")]
            if not active_buttons:
                logger.info(
                    "All %d unique action button(s) are disabled — skipping testActionButtons",
                    len(unique_buttons),
                )
                results.append(FormTestResult(
                    element_type="BUTTON",
                    group_name="dropdown_actions",
                    label=f"{len(unique_buttons)} button(s) all disabled",
                    action="button_disabled_after_dropdown",
                    success=False,
                    error_message="All action buttons disabled; no dropdown dependency satisfied",
                ))
                return results

            for b in active_buttons:
                try:
                    clicked = await self.page.evaluate("""
                        (b) => {
                            const sels = 'button, input[type="button"], input[type="submit"], [role="button"], a';
                            const el = Array.from(document.querySelectorAll(sels))[b.idx];
                            if (!el) return false;
                            el.click();
                            return true;
                        }
                    """, b)
                    results.append(FormTestResult(
                        element_type="BUTTON",
                        group_name="dropdown_actions",
                        label=b.get("text", ""),
                        action=f"button_click:{b.get('kind')}",
                        success=bool(clicked),
                    ))
                    await asyncio.sleep(0.3)
                except Exception as exc:
                    results.append(FormTestResult(
                        element_type="BUTTON",
                        group_name="dropdown_actions",
                        label=b.get("text", ""),
                        action="button_click:error",
                        success=False,
                        error_message=str(exc)[:300],
                    ))
        except Exception as exc:
            logger.debug("Action button test failed: %s", exc)
        return results

    async def resetDropdownState(self) -> FormTestResult:
        """
        Reset dropdown state to defaults via clear/reset controls and native fallback.
        """
        try:
            await self.page.evaluate("""
                () => {
                    const txt = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase();
                    const clearBtn = Array.from(document.querySelectorAll('button, input[type="button"], [role="button"], a'))
                        .find(el => {
                            const t = txt(el);
                            return t.includes('clear') || t.includes('reset');
                        });
                    if (clearBtn && !(clearBtn.disabled || clearBtn.getAttribute('aria-disabled') === 'true')) {
                        clearBtn.click();
                    }
                    document.querySelectorAll('select').forEach((el) => {
                        if (el.options && el.options.length > 0) {
                            el.selectedIndex = 0;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    });
                }
            """)
            await asyncio.sleep(0.5)
            return FormTestResult(
                element_type="DROPDOWN",
                group_name="dynamic_chain",
                label="reset",
                action="dropdown_reset:verified",
                success=True,
            )
        except Exception as exc:
            return FormTestResult(
                element_type="DROPDOWN",
                group_name="dynamic_chain",
                label="reset",
                action="dropdown_reset:error",
                success=False,
                error_message=str(exc)[:300],
            )
    # ===== DROPDOWN TEST END =====

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
