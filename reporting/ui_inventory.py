"""
reporting/ui_inventory.py — Intelligent UI element inventory with functionality classification.

Enriches ElementInfo objects with DOM metadata (xpath, parent container, DOM depth)
and classifies each element into a functionality module (Search, Navigation, Form, etc.).

Output: output/reports/{run_id}_ui_inventory.json
Grouped by: functionality module (Navigation, Search, Authentication, Actions, etc.)

Design:
  - FunctionClassifier: pure-Python rule-based, zero extra DOM queries
  - DOM enrichment (xpath/parent/depth): ONE JS call per element via async page.evaluate()
  - All errors caught gracefully — never blocks the crawl run
  - UIInventory.ingest() is called once per page after discover()
  - UIInventory.save() is called once at end of run()
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Keyword helper
# ══════════════════════════════════════════════════════════════════════════════

def _kw(text: str, keywords: list) -> bool:
    """True if any keyword appears in text (case-insensitive)."""
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in keywords)


# ══════════════════════════════════════════════════════════════════════════════
# Classification rules — pure Python, no DOM queries
# Applied in ORDER; first match wins.
# Each entry: (matcher_fn, category, module_name)
# ══════════════════════════════════════════════════════════════════════════════

_RULES: list = [
    # START: Intelligent UI Analysis & Navigation Testing

    # ── Authentication ────────────────────────────────────────────────────────
    (lambda e: e.input_type == "password",
     "Authentication", "Password Field"),
    (lambda e: e.element_type.name == "INPUT_EMAIL" and _kw(e.label, ["login", "email", "user"]),
     "Authentication", "Login / Auth Form"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["login", "sign in", "signin", "log in"]),
     "Authentication", "Login Button"),

    # ── Search ────────────────────────────────────────────────────────────────
    (lambda e: e.input_type == "search" or _kw(e.label, ["search", "find", "query", "lookup"]),
     "Search", "Search Input"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["search", "find", "lookup"]),
     "Search", "Search Trigger"),

    # ── Navigation ────────────────────────────────────────────────────────────
    (lambda e: e.element_type.name == "NAV_ITEM",
     "Navigation", "Sidebar Navigation"),
    (lambda e: e.element_type.name == "TAB",
     "Navigation", "Tab Navigation"),
    (lambda e: e.element_type.name == "LINK",
     "Navigation", "Navigation Link"),

    # ── File Management ───────────────────────────────────────────────────────
    (lambda e: e.element_type.name == "FILE_UPLOAD",
     "File Management", "File Upload"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["upload", "import", "attach"]),
     "File Management", "Upload Trigger"),

    # ── Form Controls ─────────────────────────────────────────────────────────
    (lambda e: e.element_type.name == "CHECKBOX",
     "Form Controls", "Checkbox / Toggle"),
    (lambda e: e.element_type.name == "RADIO",
     "Form Controls", "Radio Selection"),
    (lambda e: e.element_type.name == "SELECT",
     "Form Controls", "Dropdown / Select"),
    (lambda e: e.element_type.name == "TEXTAREA",
     "Form Controls", "Text Area / Comment"),
    (lambda e: e.input_type == "date",
     "Form Controls", "Date Picker"),
    (lambda e: e.input_type == "time",
     "Form Controls", "Time Picker"),
    (lambda e: e.input_type == "range",
     "Form Controls", "Range Slider"),

    # ── Data Entry ────────────────────────────────────────────────────────────
    (lambda e: e.element_type.name.startswith("INPUT_"),
     "Data Entry", "Input Field"),

    # ── Actions ───────────────────────────────────────────────────────────────
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["submit", "save", "create", "add", "new", "publish"]),
     "Actions", "Form Submission"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["export", "download", "print", "pdf", "excel", "csv"]),
     "Actions", "Data Export"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["edit", "update", "modify", "change", "rename"]),
     "Actions", "Edit / Update"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["delete", "remove", "clear", "reset", "purge", "wipe"]),
     "Actions", "Destructive Action"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["filter", "sort", "group", "order"]),
     "Actions", "Filter / Sort"),
    (lambda e: e.element_type.name == "BUTTON" and _kw(e.label, ["modal", "popup", "dialog", "open", "show"]),
     "Actions", "Modal Trigger"),
    (lambda e: e.element_type.name == "BUTTON",
     "Actions", "Action Button"),

    # ── Fallback ──────────────────────────────────────────────────────────────
    (lambda e: True,
     "Other", "Unclassified"),

    # END: Intelligent UI Analysis & Navigation Testing
]


def _classify(elem) -> tuple:
    """Return (category, module_name) for an element. First matching rule wins."""
    for matcher, category, module in _RULES:
        try:
            if matcher(elem):
                return category, module
        except Exception:
            continue
    return "Other", "Unclassified"


# ══════════════════════════════════════════════════════════════════════════════
# DOM metadata — single JS call per element (xpath, parent info, depth)
# ══════════════════════════════════════════════════════════════════════════════

_DOM_META_JS = """
el => {
    const getXPath = node => {
        if (node.id) return '//*[@id="' + node.id + '"]';
        const parts = [];
        let cur = node;
        while (cur && cur.nodeType === 1 && cur.tagName.toUpperCase() !== 'HTML') {
            const tag = cur.tagName.toLowerCase();
            const sibs = cur.parentNode
                ? Array.from(cur.parentNode.children).filter(s => s.tagName === cur.tagName)
                : [];
            const pos = sibs.length > 1 ? '[' + (sibs.indexOf(cur) + 1) + ']' : '';
            parts.unshift(tag + pos);
            cur = cur.parentNode;
        }
        return '/html/' + parts.join('/');
    };
    const par = el.parentElement;
    let depth = 0, d = el;
    while (d.parentElement) { depth++; d = d.parentElement; }
    return {
        xpath: getXPath(el),
        depth: depth,
        parent: par ? {
            tag:        par.tagName.toLowerCase(),
            id:         par.id || '',
            class:      (par.className || '').split(/\\s+/).filter(Boolean).slice(0, 5).join(' '),
            role:       par.getAttribute('role') || '',
            aria_label: par.getAttribute('aria-label') || ''
        } : {}
    };
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Detection event logging
# ══════════════════════════════════════════════════════════════════════════════

def _log_detection(elem, module: str) -> None:
    """Emit structured [INFO] detection log per element type."""
    t   = elem.element_type.name
    lbl = elem.label or elem.attrs.get("aria-label", "") or elem.selector

    if t == "NAV_ITEM":
        logger.info("[UI Inventory] Sidebar detected: %s → %s", lbl, module)
    elif t == "CHECKBOX":
        logger.info("[UI Inventory] Checkbox detected: %s", lbl)
    elif t == "RADIO":
        logger.info("[UI Inventory] Radio button detected: %s", lbl)
    elif t == "SELECT":
        logger.info("[UI Inventory] Dropdown identified: %s", lbl)
    elif t == "FILE_UPLOAD":
        logger.info("[UI Inventory] Upload button detected: %s", lbl)
    elif t == "TAB":
        logger.info("[UI Inventory] Tab navigation detected: %s", lbl)
    elif "INPUT" in t or t == "TEXTAREA":
        logger.info("[UI Inventory] Form section detected: [%s] %s → %s", t, lbl, module)
    elif t == "BUTTON":
        logger.info("[UI Inventory] Function identified: %s → %s", lbl, module)


# ══════════════════════════════════════════════════════════════════════════════
# UIInventory — main class
# ══════════════════════════════════════════════════════════════════════════════

class UIInventory:
    """
    Intelligent UI element inventory with functionality classification.

    Usage (mirrors ExcelReporter / ReportBuilder pattern):
        inventory = UIInventory(run_id, target_url)
        await inventory.ingest(url, elements, page)   # once per page
        inventory_path = inventory.save()              # once at end of run
    """

    def __init__(self, run_id: str, target_url: str):
        self.run_id     = run_id
        self.target_url = target_url
        self._total     = 0
        self._pages: int = 0
        # Grouped by module_name → {category, module, count, elements:[]}
        self._by_module: Dict[str, dict] = {}
        # Dedup across pages: fingerprints already ingested
        self._seen_fps: set = set()

        report_dir = config.OUTPUT_DIR / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = report_dir / f"{run_id}_ui_inventory.json"
        self.saved_path  = ""

    # ── Public API ────────────────────────────────────────────────────────────

    async def ingest(self, url: str, elements: list, page) -> None:
        """
        Enrich and classify all elements from a single page.
        Call once per page BEFORE the interaction loop starts.
        """
        if not elements:
            return

        self._pages += 1
        logger.info("[UI Inventory] Full page inspection started: %s", url)
        logger.info("[UI Inventory] Total interactive elements collected: %d", len(elements))

        # START: Intelligent UI Analysis & Navigation Testing
        new_count = 0
        for idx, elem in enumerate(elements, start=1):

            # Skip elements already ingested (same fingerprint from another page)
            fp = elem.fingerprint or f"{elem.element_type.name}|{elem.selector}|{elem.label}"
            if fp in self._seen_fps:
                continue
            self._seen_fps.add(fp)

            logger.info("[UI Inventory] Processing element %d/%d", idx, len(elements))

            # Enrich: single JS call for xpath + parent + depth
            try:
                meta = await page.evaluate(_DOM_META_JS, elem.handle)
            except Exception as exc:
                logger.debug("[UI Inventory] DOM meta skipped for [%s]: %s",
                             elem.element_type.name, exc)
                meta = {"xpath": "", "depth": 0, "parent": {}}

            # Classify by functionality
            category, module = _classify(elem)

            # Build inventory record
            record = {
                "url":         url,
                "type":        elem.element_type.name,
                "category":    category,
                "module":      module,
                "label":       elem.label,
                "selector":    elem.selector,
                "xpath":       meta.get("xpath", ""),
                "tag":         elem.tag,
                "input_type":  elem.input_type,
                "href":        elem.href,
                "fingerprint": fp,
                "id":          elem.attrs.get("id", ""),
                "name":        elem.attrs.get("name", ""),
                "aria_label":  elem.attrs.get("aria-label", ""),
                "placeholder": elem.attrs.get("placeholder", ""),
                "class":       elem.attrs.get("class", ""),
                "role":        elem.attrs.get("role", ""),
                "dom_depth":   meta.get("depth", 0),
                "parent":      meta.get("parent", {}),
                "timestamp":   _now(),
            }

            # Emit detection log
            _log_detection(elem, module)

            # Group into module bucket
            if module not in self._by_module:
                self._by_module[module] = {
                    "category": category,
                    "module":   module,
                    "count":    0,
                    "elements": [],
                }
            self._by_module[module]["elements"].append(record)
            self._by_module[module]["count"] += 1
            self._total += 1
            new_count   += 1

        logger.info(
            "[UI Inventory] Element inventory created successfully: "
            "%d new elements from %s", new_count, url
        )
        # END: Intelligent UI Analysis & Navigation Testing

    def save(self) -> str:
        """
        Write the structured JSON inventory to disk.
        Returns the file path string, or empty string on failure.
        """
        if not self._by_module:
            logger.info("[UI Inventory] Nothing to save — inventory is empty.")
            return ""

        output = {
            "meta": {
                "run_id":         self.run_id,
                "target_url":     self.target_url,
                "generated_at":   _now(),
                "total_elements": self._total,
                "total_modules":  len(self._by_module),
                "total_pages":    self._pages,
            },
            # Sort modules alphabetically for reproducible output
            "modules": {
                k: v for k, v in sorted(self._by_module.items())
            },
        }

        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            logger.info(
                "[UI Inventory] Inventory saved: %s  (%d elements across %d modules)",
                self.report_path, self._total, len(self._by_module),
            )
            self.saved_path = str(self.report_path)
            return self.saved_path
        except Exception as exc:
            logger.error("[UI Inventory] Failed to save inventory: %s", exc)
            return ""

    def get_summary(self) -> dict:
        """Return a compact summary dict (for banner/console output)."""
        return {
            "total_elements": self._total,
            "total_modules":  len(self._by_module),
            "modules": {k: v["count"] for k, v in self._by_module.items()},
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
