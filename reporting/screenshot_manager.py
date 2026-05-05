"""
reporting/screenshot_manager.py — Manages error screenshot capture.

Saves screenshots into structured subdirectories per run and URL.
"""

import logging
import re
from pathlib import Path

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config

logger = logging.getLogger(__name__)


class ScreenshotManager:
    """
    Captures and saves error screenshots during test execution.
    Directory layout: output/screenshots/{run_id}/{sanitized_url}/error_{n}_{type}.png
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._counters: dict = {}   # per-url error counter

    def _sanitize(self, url: str) -> str:
        """Convert URL to a safe directory name."""
        # Strip scheme, replace non-alphanumeric chars with underscores
        name = re.sub(r"https?://", "", url)
        name = re.sub(r"[^\w\-]", "_", name)
        return name[:80].strip("_")

    def _screenshot_dir(self, url: str) -> Path:
        d = config.SCREENSHOT_DIR / self.run_id / self._sanitize(url)
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def capture_error(
        self,
        page,
        url: str,
        action: str,
        error_type: str,
    ) -> str:
        """
        Take a screenshot of the current page state and save it.
        Returns the absolute path as a string, or empty string on failure.
        """
        key = self._sanitize(url)
        self._counters[key] = self._counters.get(key, 0) + 1
        n = self._counters[key]

        # Build a filename from the error type and action
        safe_action = re.sub(r"[^\w]", "_", str(action))[:40]
        safe_type   = re.sub(r"[^\w]", "_", str(error_type))[:30]
        filename    = f"error_{n:03d}_{safe_type}_{safe_action}.png"
        filepath    = self._screenshot_dir(url) / filename

        try:
            await page.screenshot(path=str(filepath), full_page=False)
            logger.info("Screenshot saved: %s", filepath)
            return str(filepath)
        except Exception as exc:
            logger.warning("Screenshot capture failed: %s", exc)
            return ""
