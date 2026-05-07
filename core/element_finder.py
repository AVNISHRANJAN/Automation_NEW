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
    # ===== Sidebar Detection Enhancement START =====
    NAV_ITEM    = auto()   # sidebar/nav clickable: div, span, li, SVG, icon, onclick
    # ===== Sidebar Detection Enhancement END =====


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
    # START: Sidebar Navigation Processing
    fingerprint: str = ""           # Structural identity key — survives re-discovery
    # END: Sidebar Navigation Processing


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


# ===== Sidebar Detection Enhancement START =====
# Selectors that catch dashboard/admin sidebar navigation patterns missed by
# DISCOVERY_SELECTORS.  Ordered from most-specific to least-specific to keep
# the dedup set effective and avoid redundant work.
#
# Design rules:
#   - Every selector here is ADDITIVE — it complements, never replaces, the
#     existing DISCOVERY_SELECTORS list.
#   - The visibility + bounding-box filter in discover() still applies.
#   - Elements already captured by DISCOVERY_SELECTORS are deduplicated via the
#     shared __playwright_id__ mechanism.
SIDEBAR_SELECTORS: list = [
    # onclick-driven elements (React/Vue event handlers, admin panels)
    ("[onclick]:not(input):not(button):not(a):not(select)",   ElementType.NAV_ITEM),

    # aria-label elements that are not already covered (icon-only nav items)
    ("[aria-label]:not(input):not(button):not(a):not([role='button'])",  ElementType.NAV_ITEM),

    # Keyboard-accessible custom elements (tabindex >= 0, not standard controls)
    ("[tabindex]:not(input):not(button):not(a):not(select):not(textarea)", ElementType.NAV_ITEM),

    # Common nav/menu class patterns used by Bootstrap, AdminLTE, CoreUI, etc.
    (".nav-item",      ElementType.NAV_ITEM),
    (".nav-link",      ElementType.NAV_ITEM),
    (".menu-item",     ElementType.NAV_ITEM),
    (".sidebar-item",  ElementType.NAV_ITEM),
    (".sidebar-link",  ElementType.NAV_ITEM),

    # Generic class substring patterns (covers icon-, menu-, nav- prefixes)
    ("[class*='nav-']:not(input):not(button):not(a)",           ElementType.NAV_ITEM),
    ("[class*='menu-']:not(input):not(button):not(a)",          ElementType.NAV_ITEM),
    ("[class*='sidebar']:not(input):not(button):not(a)",        ElementType.NAV_ITEM),

    # SVG-based navigation icons (common in Tailwind/Material/Ant Design sidebars)
    ("svg[role='img'][aria-label]",                             ElementType.NAV_ITEM),

    # Font Awesome / Material icon elements used as nav triggers
    ("i[class*='fa']:not([aria-hidden='true'])",               ElementType.NAV_ITEM),
    ("i[class*='icon']:not([aria-hidden='true'])",             ElementType.NAV_ITEM),
    ("span[class*='icon']:not([aria-hidden='true'])",          ElementType.NAV_ITEM),

    # role='menuitem' and role='navigation' direct children
    ("[role='menuitem']",  ElementType.NAV_ITEM),
    ("[role='navigation'] > *",  ElementType.NAV_ITEM),
    ("nav > *",            ElementType.NAV_ITEM),
    ("nav li",             ElementType.NAV_ITEM),
    ("aside li",           ElementType.NAV_ITEM),
    ("aside a",            ElementType.NAV_ITEM),
]
# ===== Sidebar Detection Enhancement END =====


# START: Sidebar Navigation Processing
def _compute_fingerprint(
    elem_type: ElementType,
    selector: str,
    label: str,
    href: str,
    attrs: dict,
) -> str:
    """
    Build a structural fingerprint that uniquely identifies an element
    across re-discovery cycles (page drift → back-navigation rebuilds DOM).

    Priority order for uniqueness:
      id > aria-label > href > data-testid/data-id > label + class prefix

    Intentionally does NOT use __playwright_id__ (random float) because
    that resets every time the DOM is rebuilt.
    """
    elem_id    = attrs.get("id", "") or ""
    aria_label = attrs.get("aria-label", "") or ""
    cls        = (attrs.get("class", "") or "")[:40]
    name       = attrs.get("name", "") or ""
    data_id    = attrs.get("data-testid", "") or attrs.get("data-id", "") or ""
    safe_label = label[:40] if label else ""
    safe_href  = href[:80] if href else ""
    return (
        f"{elem_type.name}|"
        f"{selector}|"
        f"{elem_id}|"
        f"{aria_label}|"
        f"{safe_href}|"
        f"{safe_label}|"
        f"{cls}|"
        f"{name}|"
        f"{data_id}"
    )
# END: Sidebar Navigation Processing


class ElementFinder:

    def __init__(self, page: Page):
        self.page = page
        # ===== Sidebar Detection Enhancement START =====
        # Shared dedup set persisted across discover() → discover_sidebar_elements()
        # so both passes share one identity namespace.
        self._seen_handles: set = set()
        # ===== Sidebar Detection Enhancement END =====

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

                    # START: Sidebar Navigation Processing
                    fingerprint = _compute_fingerprint(
                        elem_type, selector, label, href, attrs
                    )
                    # END: Sidebar Navigation Processing

                    info = ElementInfo(
                        element_type=elem_type,
                        selector=selector,
                        handle=handle,
                        label=label,
                        href=href,
                        tag=tag,
                        input_type=input_type,
                        attrs=attrs,
                        fingerprint=fingerprint,
                    )
                    found.append(info)

                except Exception as exc:
                    logger.debug("Element inspection error: %s", exc)
                    continue

        # ===== Sidebar Detection Enhancement START =====
        # Persist seen_handles on self so discover_sidebar_elements() can
        # reference the same dedup set without re-scanning known elements.
        self._seen_handles = seen_handles
        # ===== Sidebar Detection Enhancement END =====
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

    # ===== Sidebar Detection Enhancement START =====
    async def discover_sidebar_elements(
        self, seen_handles: set
    ) -> List["ElementInfo"]:
        """
        Discover sidebar/navigation elements that DISCOVERY_SELECTORS misses.

        Strategy:
          1. Short DOM-stabilisation wait so React/Vue sidebars have rendered.
          2. Attempt to expand hover-triggered sidebars via JS mouseover dispatch.
          3. Run SIDEBAR_SELECTORS through the same visibility + dedup pipeline
             as discover(), sharing the caller-supplied seen_handles set so
             elements already captured by discover() are not duplicated.
          4. Safe: every operation is wrapped; exceptions skip the element.

        Args:
            seen_handles: The __playwright_id__ set from the preceding
                          discover() call.  Mutated in-place to add new IDs.

        Returns:
            List of ElementInfo for newly discovered sidebar/nav elements.
        """
        found: List[ElementInfo] = []

        # Step 1: Brief DOM-stabilisation — wait up to 800ms for pending
        # microtasks/mutations (React setState, Vue nextTick, Angular CD).
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=800)
        except Exception:
            pass   # already loaded — safe to continue

        # Step 2: Dispatch mouseover on likely sidebar containers to expand
        # hover-triggered menus (common in Bootstrap/AdminLTE sidebars).
        try:
            await self.page.evaluate("""
                () => {
                    const triggers = [
                        ...document.querySelectorAll(
                            'nav, aside, [class*=sidebar], [class*=sidenav],
                             [class*=left-menu], [class*=left-nav], [id*=sidebar]'
                        )
                    ];
                    triggers.forEach(el => {
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                    });
                }
            """)
            logger.debug("[Sidebar Detection] Dispatched hover events on sidebar containers")
        except Exception as exc:
            logger.debug("[Sidebar Detection] Hover dispatch skipped: %s", exc)

        # Step 3: Run SIDEBAR_SELECTORS through the same pipeline as discover()
        for selector, elem_type in SIDEBAR_SELECTORS:
            try:
                handles = await self.page.query_selector_all(selector)
            except Exception as exc:
                logger.debug("[Sidebar Detection] Selector '%s' failed: %s", selector, exc)
                continue

            for handle in handles:
                try:
                    # Visibility + bounding-box — same rules as discover()
                    box = await handle.bounding_box()
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue

                    is_visible = await handle.is_visible()
                    if not is_visible:
                        logger.debug(
                            "[Sidebar Detection] Hidden sidebar item skipped (selector: %s)",
                            selector
                        )
                        continue

                    # Deduplicate using the shared seen_handles set from discover()
                    js_id = await self.page.evaluate(
                        "el => el.__playwright_id__ || "
                        "(el.__playwright_id__ = Math.random())",
                        handle,
                    )
                    if js_id in seen_handles:
                        continue
                    seen_handles.add(js_id)

                    label      = await self._infer_label_sidebar(handle, elem_type)
                    attrs      = await self._collect_attrs(handle)
                    tag        = await self.page.evaluate(
                        "el => el.tagName.toLowerCase()", handle
                    )
                    input_type = await handle.get_attribute("type") or ""
                    href       = ""
                    if tag == "a":
                        href = await handle.get_attribute("href") or ""

                    # START: Sidebar Navigation Processing
                    fingerprint = _compute_fingerprint(
                        elem_type, selector, label, href, attrs
                    )
                    # END: Sidebar Navigation Processing

                    info = ElementInfo(
                        element_type=elem_type,
                        selector=selector,
                        handle=handle,
                        label=label,
                        href=href,
                        tag=tag,
                        input_type=input_type,
                        attrs=attrs,
                        fingerprint=fingerprint,
                    )
                    found.append(info)
                    logger.info(
                        "[Sidebar Detection] Interactive sidebar element found: [%s] %s",
                        tag, label or selector
                    )

                except Exception as exc:
                    logger.debug("[Sidebar Detection] Element inspection error: %s", exc)
                    continue

        if found:
            logger.info(
                "[Sidebar Detection] %d sidebar/nav element(s) discovered on %s",
                len(found), self.page.url
            )
        return found

    async def _infer_label_sidebar(self, handle: ElementHandle, elem_type: ElementType) -> str:
        """
        Extended label inference for sidebar/icon elements.

        Adds SVG-specific strategies on top of the standard _infer_label:
          - SVG <title> element text
          - xlink:href / href attribute of <use> child (icon sprite reference)
          - Class-name fragment as last resort for icon-only elements

        This ensures icon-only nav items get a meaningful label instead of
        falling back to the enum name ('nav_item').
        """
        try:
            val = await self.page.evaluate(
                """
                el => {
                    const a = s => (el.getAttribute(s) || '').trim();

                    // Standard strategies (same order as _infer_label)
                    const standard =
                        a('aria-label')
                        || a('title')
                        || (el.textContent || '').trim()
                        || a('placeholder')
                        || a('name')
                        || a('id')
                        || '';
                    if (standard) return standard;

                    // SVG <title> child
                    const svgTitle = el.querySelector && el.querySelector('title');
                    if (svgTitle && svgTitle.textContent)
                        return svgTitle.textContent.trim();

                    // <use href> icon sprite reference (e.g. '#icon-home')
                    const useEl = el.querySelector && el.querySelector('use');
                    if (useEl) {
                        const ref = useEl.getAttribute('href')
                                 || useEl.getAttribute('xlink:href') || '';
                        if (ref) return ref.replace('#', '');
                    }

                    // Class fragment: e.g. 'fa-home' → 'home'
                    const cls = el.className || '';
                    const match = cls.match(/(?:fa|icon|nav|menu)-([\\w-]+)/);
                    if (match) return match[1];

                    return '';
                }
                """,
                handle,
            )
            if val and val.strip():
                return val.strip()[:80]
        except Exception:
            pass
        # Final fallback: use enum name so it's never empty
        return elem_type.name.lower()
    # ===== Sidebar Detection Enhancement END =====

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
