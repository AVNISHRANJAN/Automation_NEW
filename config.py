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

# ── Crawler ────────────────────────────────────────────────────────────────────
MAX_PAGES       = int(os.getenv("MAX_PAGES", "100"))
SAME_DOMAIN_ONLY = True   # Do not follow external links

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
