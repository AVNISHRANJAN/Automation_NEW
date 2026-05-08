"""
core/state_tracker.py — Crawl-wide deduplication state for Web Auto Tester.

Maintains five independent tracking sets that persist for the ENTIRE duration
of a crawl run, ensuring every unique UI element, interaction, route, and UI
state (modal/dropdown/sidebar) is tested EXACTLY ONCE regardless of how many
pages it appears on.

Design:
  - StateTracker is instantiated ONCE in Crawler.__init__() and shared with
    FormTester and every sub-system that performs interactions.
  - All check/mark operations are O(1) hash-set lookups.
  - Keys are structural fingerprints (DOM-stable) rather than transient IDs,
    so they survive page reloads, drift-recovery, and back-navigation.
  - Thread-safe by design (single async event loop; no concurrency).

Tracking sets:
  visited_pages       — normalized URLs already crawled
  tested_elements     — element fingerprints already interacted with (global)
  tested_interactions — (fingerprint, element_type) pairs already tested
  tested_routes       — alias of visited_pages for explicit route semantics
  tested_states       — modal / dropdown / sidebar state keys already processed

Console conventions (logged at DEBUG level for each skip):
  "Skipping already tested element"
  "Skipping previously tested route"
  "Skipping duplicate interaction"
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StateTracker:
    """
    Single source of truth for deduplication across an entire crawl run.

    Usage in Crawler:
        self.state_tracker = StateTracker()

        # Before interacting:
        if self.state_tracker.is_element_tested(elem_key):
            logger.debug("Skipping already tested element: %s", elem.label)
            continue

        # After interacting:
        self.state_tracker.mark_element_tested(elem_key)
        self.state_tracker.mark_interaction_tested(elem_key, elem.element_type.name)
    """

    def __init__(self) -> None:
        # Five orthogonal tracking sets —————————————————————————————————————
        self.visited_pages:       set = set()  # normalized URLs
        self.tested_elements:     set = set()  # element fingerprints (crawl-wide)
        self.tested_interactions: set = set()  # (fingerprint, elem_type_name) tuples
        self.tested_routes:       set = set()  # alias for visited_pages (explicit)
        self.tested_states:       set = set()  # modal / dropdown / sidebar state keys

    # ── Page / Route ───────────────────────────────────────────────────────────

    def is_page_visited(self, url: str) -> bool:
        """Return True if this normalized URL has already been crawled."""
        return url in self.visited_pages

    def mark_page_visited(self, url: str) -> None:
        """Record a page as crawled. Populates both visited_pages and tested_routes."""
        self.visited_pages.add(url)
        self.tested_routes.add(url)

    # ── Element (global) ───────────────────────────────────────────────────────

    def is_element_tested(self, key: str) -> bool:
        """
        Return True if an element with this fingerprint key has already been
        interacted with on ANY page during this run.

        Log message: "Skipping already tested element"
        """
        return key in self.tested_elements

    def mark_element_tested(self, key: str) -> None:
        """Record an element fingerprint as globally tested."""
        self.tested_elements.add(key)

    # ── Interaction ────────────────────────────────────────────────────────────

    def is_interaction_tested(self, key: str, elem_type_name: str) -> bool:
        """
        Return True if the exact (fingerprint, element_type) pair was already
        exercised — prevents re-clicking the same button type on the same element.

        Log message: "Skipping duplicate interaction"
        """
        return (key, elem_type_name) in self.tested_interactions

    def mark_interaction_tested(self, key: str, elem_type_name: str) -> None:
        """Record a (fingerprint, element_type) interaction pair as tested."""
        self.tested_interactions.add((key, elem_type_name))

    # ── UI State (modal / dropdown / sidebar) ──────────────────────────────────

    def build_state_key(
        self,
        page_url: str,
        state_type: str,
        identifier: str = "",
    ) -> str:
        """
        Compose a deterministic string key for a UI state.

        Examples:
            build_state_key(url, "modal", "0")    → "https://…::modal::0"
            build_state_key(url, "dropdown", "")  → "https://…::dropdown::"
            build_state_key(url, "checkbox_group", "newsletter") → "…::checkbox_group::newsletter"
        """
        return f"{page_url}::{state_type}::{identifier}"

    def is_state_tested(self, state_key: str) -> bool:
        """
        Return True if this UI state (modal/dropdown/sidebar group) has already
        been processed on this crawl.

        Log message: "Skipping duplicate interaction"
        """
        return state_key in self.tested_states

    def mark_state_tested(self, state_key: str) -> None:
        """Record a UI state key as processed."""
        self.tested_states.add(state_key)

    # ── Convenience helpers ────────────────────────────────────────────────────

    def get_element_key(self, info) -> str:  # type: ignore[return]
        """
        Extract the deduplication key from an ElementInfo instance.

        Uses the structural fingerprint when present (preferred), falls back
        to a '|'-joined tuple of (selector, label, type) for fingerprint-less
        elements (e.g. bare iframe elements).
        """
        if getattr(info, "fingerprint", None):
            return info.fingerprint
        return f"{info.selector}|{info.label}|{info.element_type.name}"

    def summary(self) -> dict:
        """Return a snapshot of current dedup counters (for logging / reporting)."""
        return {
            "visited_pages":       len(self.visited_pages),
            "tested_elements":     len(self.tested_elements),
            "tested_interactions": len(self.tested_interactions),
            "tested_states":       len(self.tested_states),
        }
