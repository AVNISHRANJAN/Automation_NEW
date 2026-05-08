"""
config.py — Central configuration. Never hardcode values in core modules.
All timeouts, paths, dummy data, and browser settings live here.
"""

from pathlib import Path
import os

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"
SCREENSHOT_DIR = OUTPUT_DIR / "screenshots"
REPORT_DIR     = OUTPUT_DIR / "reports"

# ── Browser ────────────────────────────────────────────────────────────────────
HEADLESS        = os.getenv("HEADLESS", "false").lower() == "true"
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "30000"))   # ms
NAV_TIMEOUT     = int(os.getenv("NAV_TIMEOUT", "30000"))        # ms
ACTION_TIMEOUT  = int(os.getenv("ACTION_TIMEOUT", "10000"))     # ms
SLOW_MO         = int(os.getenv("SLOW_MO", "100"))              # ms between actions

# ── Crawler ───────────────────────────────────────────────────────────────────
MAX_PAGES       = int(os.getenv("MAX_PAGES", "100"))
SAME_DOMAIN_ONLY = True   # Do not follow external links

# ── State tracking / global dedup ─────────────────────────────────────────────
# When True: elements whose fingerprint was already interacted with on ANY
# previous page are skipped immediately (crawl-wide deduplication).
# Set to False to revert to per-page-only deduplication.
GLOBAL_ELEMENT_DEDUP = os.getenv("GLOBAL_ELEMENT_DEDUP", "true").lower() == "true"

# ── Dummy data for form filling ────────────────────────────────────────────────
DUMMY_DATA = {
    "email":    "tester@example.com",
    "name":     "Test User",
    "username": "testuser",
    "password": "Test@12345",
    "phone":    "9876543210",
    "address":  "123 Test Street",
    "city":     "Test City",
    "zip":      "110001",
    "search":   "test query",
    "message":  "This is an automated test message.",
    "default":  "test_value",
    # ===== NEW FEATURE START =====
    # Added for new input types discovered by enhanced element_finder
    "url":      "https://example.com",
    "date":     "2025-01-15",
    "time":     "10:30",
    "range":    "50",
    # ===== NEW FEATURE END =====
}

# ── Login detection heuristics ─────────────────────────────────────────────────
LOGIN_SELECTORS = [
    'input[type="password"]',
    'input[name*="pass"]',
    'input[id*="pass"]',
    'form[action*="login"]',
    'form[action*="signin"]',
    'button[type="submit"]',
]

LOGIN_URL_KEYWORDS = ["login", "signin", "sign-in", "auth", "account/login"]

# ── Post-login detection ───────────────────────────────────────────────────────
# Wait for URL to change OR password field to disappear
POST_LOGIN_WAIT_TIMEOUT = 120_000  # 2 minutes for user to log in manually

# ── Dead-click detection ───────────────────────────────────────────────────────
# If no DOM mutation / URL change occurs within this window after a click,
# the interaction is classified as a DEAD_CLICK (logged, not a failure).
DEAD_CLICK_TIMEOUT = int(os.getenv("DEAD_CLICK_TIMEOUT", "800"))  # ms

# ── Modal / recursive UI ───────────────────────────────────────────────────────
# Maximum depth of recursive modal/accordion/drawer testing.
# Prevents infinite nesting when modals open other modals.
MAX_MODAL_DEPTH = int(os.getenv("MAX_MODAL_DEPTH", "3"))

# Time (ms) to wait after expanding an accordion before re-scanning for elements.
ACCORDION_EXPAND_WAIT = int(os.getenv("ACCORDION_EXPAND_WAIT", "400"))

# ── Shadow DOM traversal ───────────────────────────────────────────────────────
# Enable/disable shadow DOM element discovery (may slow down scans on complex apps).
SHADOW_DOM_ENABLED = os.getenv("SHADOW_DOM_ENABLED", "true").lower() == "true"

# ── Scroll / lazy-load ────────────────────────────────────────────────────────
# Number of scroll steps when triggering infinite scroll / lazy sections.
SCROLL_STEPS = int(os.getenv("SCROLL_STEPS", "3"))

# ── Security scan (safe, non-destructive) ────────────────────────────────────
SECURITY_SCAN_ENABLED = os.getenv("SECURITY_SCAN_ENABLED", "true").lower() == "true"
SECURITY_MAX_SAFE_PROBES_PER_PAGE = int(os.getenv("SECURITY_MAX_SAFE_PROBES_PER_PAGE", "3"))
