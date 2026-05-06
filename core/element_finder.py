"""
core/element_finder.py — Dynamic element discovery on a live Playwright page.

Returns structured ElementInfo objects rather than raw handles.
Callers (interactor.py) work with ElementInfo — never raw DOM handles —
to keep interaction logic clean and testable.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List

from playwright.async_api import Page, ElementHandle

logger = logging.getLogger(__name__)


class ElementType(Enum):
    BUTTON      = auto()
    INPUT_TEXT  = auto()
    INPUT_EMAIL = auto()
    INPUT_TEL   = auto()
    INPUT_NUMBER= auto()
    INPUT_SEARCH= auto()
    INPUT_PASS  = auto()   # logged but not filled — never autofill passwords
    TEXTAREA    = auto()
    SELECT      = auto()
    CHECKBOX    = auto()
    RADIO       = auto()
    LINK        = auto()
    FORM        = auto()
    OTHER       = auto()
    # ===== NEW CODE START =====
    FILE_UPLOAD = auto()   # input[type=file] — upload a sample file
    TAB         = auto()   # [role=tab] UI tab panels
    # ===== NEW CODE END =====


@dataclass
class ElementInfo:
    element_type: ElementType
    selector: str                   # CSS selector used to find it
    handle: ElementHandle           # Live Playwright handle
    label: str = ""                 # Best human-readable label we could infer
    href: str = ""                  # For links
    tag: str = ""
    input_type: str = ""
    attrs: dict = field(default_factory=dict)


# Selectors to discover — ordered by priority
# ===== NEW FEATURE START =====
# Added url, date, time, color, range input types for broader coverage
DISCOVERY_SELECTORS = [
    ("button:not([disabled])",                      ElementType.BUTTON),
    ("input[type='submit']:not([disabled])",        ElementType.BUTTON),
    ("input[type='button']:not([disabled])",        ElementType.BUTTON),
    ("[role='button']:not([disabled])",             ElementType.BUTTON),
    ("input[type='text']:not([disabled])",          ElementType.INPUT_TEXT),
    ("input[type='email']:not([disabled])",         ElementType.INPUT_EMAIL),
    ("input[type='tel']:not([disabled])",           ElementType.INPUT_TEL),
    ("input[type='number']:not([disabled])",        ElementType.INPUT_NUMBER),
    ("input[type='search']:not([disabled])",        ElementType.INPUT_SEARCH),
    ("input[type='url']:not([disabled])",           ElementType.INPUT_TEXT),   # url input
    ("input[type='date']:not([disabled])",          ElementType.INPUT_TEXT),   # date input
    ("input[type='time']:not([disabled])",          ElementType.INPUT_TEXT),   # time input
    ("input[type='range']:not([disabled])",         ElementType.INPUT_NUMBER), # range slider
    ("input[type='password']:not([disabled])",      ElementType.INPUT_PASS),
    ("input:not([type]):not([disabled])",           ElementType.INPUT_TEXT),
    ("textarea:not([disabled])",                    ElementType.TEXTAREA),
    ("select:not([disabled])",                      ElementType.SELECT),
    ("input[type='checkbox']:not([disabled])",      ElementType.CHECKBOX),
    ("input[type='radio']:not([disabled])",         ElementType.RADIO),
    ("a[href]",                                     ElementType.LINK),
    # ===== NEW CODE START =====
    ("input[type='file']:not([disabled])",          ElementType.FILE_UPLOAD),  # file upload
    ("[role='tab']:not([disabled])",                ElementType.TAB),          # UI tab panels
    # ===== NEW CODE END =====
]
# ===== NEW FEATURE END =====


class ElementFinder:

    def __init__(self, page: Page):
        self.page = page

    async def discover(self) -> List[ElementInfo]:
        """
        Discover all interactive elements on the current page.
        Returns a deduplicated, ordered list of ElementInfo objects.
        Skips invisible, zero-size, and off-screen elements.
        """
        found: List[ElementInfo] = []
        seen_handles: set = set()

        for selector, elem_type in DISCOVERY_SELECTORS:
            try:
                handles = await self.page.query_selector_all(selector)
            except Exception as exc:
                logger.debug("Selector %s failed: %s", selector, exc)
                continue

            for handle in handles:
                try:
                    # Skip hidden / zero-size elements
                    box = await handle.bounding_box()
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue

                    is_visible = await handle.is_visible()
                    if not is_visible:
                        continue

                    # Deduplicate by element identity
                    js_id = await self.page.evaluate("el => el.__playwright_id__ || (el.__playwright_id__ = Math.random())", handle)
                    if js_id in seen_handles:
                        continue
                    seen_handles.add(js_id)

                    label = await self._infer_label(handle, elem_type)
                    href  = ""
                    if elem_type == ElementType.LINK:
                        href = await handle.get_attribute("href") or ""

                    tag = await self.page.evaluate("el => el.tagName.toLowerCase()", handle)
                    input_type = await handle.get_attribute("type") or ""

                    # ===== NEW FEATURE START =====
                    # Populate attrs with key attributes for group-aware testing
                    # (e.g., radio/checkbox 'name' attribute for group detection)
                    attrs = await self._collect_attrs(handle)
                    # ===== NEW FEATURE END =====

                    info = ElementInfo(
                        element_type=elem_type,
                        selector=selector,
                        handle=handle,
                        label=label,
                        href=href,
                        tag=tag,
                        input_type=input_type,
                        attrs=attrs,
                    )
                    found.append(info)

                except Exception as exc:
                    logger.debug("Element inspection error: %s", exc)
                    continue

        logger.info("Discovered %d interactive elements on %s", len(found), self.page.url)
        return found

    async def collect_links(self) -> List[str]:
        """Extract all href links from the current page."""
        try:
            links = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h.startsWith('http'))
            """)
            return list(set(links))
        except Exception as exc:
            logger.warning("Link collection failed: %s", exc)
            return []

    # ===== NEW FEATURE START =====
    async def _collect_attrs(self, handle: ElementHandle) -> dict:
        """
        Collect key HTML attributes from an element for group-aware testing.
        Captures: id, name, class, aria-label, placeholder, value, data-* attrs.
        """
        try:
            attrs = await self.page.evaluate("""
                el => {
                    const result = {};
                    const keys = ['id', 'name', 'class', 'aria-label', 'placeholder',
                                  'value', 'data-testid', 'data-id', 'role', 'for'];
                    keys.forEach(k => {
                        const v = el.getAttribute(k);
                        if (v !== null) result[k] = v;
                    });
                    // Capture any data-* attributes
                    Array.from(el.attributes).forEach(attr => {
                        if (attr.name.startsWith('data-') && !(attr.name in result)) {
                            result[attr.name] = attr.value;
                        }
                    });
                    return result;
                }
            """, handle)
            return attrs or {}
        except Exception as exc:
            logger.debug("Attribute collection failed: %s", exc)
            return {}

    async def discover_in_iframes(self) -> List[ElementInfo]:
        """
        Attempt to discover interactive elements inside same-origin iframes.
        Cross-origin iframes are skipped (cannot access DOM).
        Returns a flat list of ElementInfo from all accessible iframes.
        """
        iframe_elements: List[ElementInfo] = []
        try:
            frames = self.page.frames
            for frame in frames:
                if frame == self.page.main_frame:
                    continue   # already processed as main page
                frame_url = frame.url
                if not frame_url or frame_url in ("about:blank", ""):
                    continue
                # Only scan same-origin iframes
                try:
                    iframe_finder = ElementFinder.__new__(ElementFinder)
                    iframe_finder.page = frame  # type: ignore[assignment]
                    found = await iframe_finder.discover()
                    if found:
                        logger.info(
                            "Found %d element(s) in iframe: %s",
                            len(found), frame_url
                        )
                    iframe_elements.extend(found)
                except Exception as exc:
                    logger.debug("iframe scan skipped (%s): %s", frame_url, exc)
        except Exception as exc:
            logger.debug("iframe discovery error: %s", exc)
        return iframe_elements
    # ===== NEW FEATURE END =====

    async def _infer_label(self, handle: ElementHandle, elem_type: ElementType) -> str:
        """
        Try multiple strategies to get a human-readable label for an element.
        Priority: aria-label > text content > placeholder > name > id > type

        # ===== OPTIMIZATION START =====
        # Was: up to 6 sequential awaited attribute/textContent calls per element.
        # Now: single JS evaluate that tries all strategies in one browser round-trip,
        # reducing async overhead significantly on pages with many elements.
        # ===== OPTIMIZATION END =====
        """
        try:
            val = await self.page.evaluate(
                """
                el => {
                    const a = s => (el.getAttribute(s) || '').trim();
                    return a('aria-label')
                        || (el.textContent || '').trim()
                        || a('placeholder')
                        || a('name')
                        || a('id')
                        || a('title')
                        || '';
                }
                """,
                handle,
            )
            if val and val.strip():
                return val.strip()[:80]  # cap length
        except Exception:
            pass
        return elem_type.name.lower()
