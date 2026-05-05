"""
reporting/metadata_logger.py — Collects and stores test action records.

Maintains an in-memory log of every action and error, and writes a
JSONL (one JSON object per line) file for machine-readable output.
"""

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config

logger = logging.getLogger(__name__)


@dataclass
class ActionRecord:
    """Represents a single tested action."""
    timestamp: str
    url: str
    element_type: str
    element_label: str
    action: str
    success: bool = True
    error_type: str = ""
    error_message: str = ""
    screenshot_path: str = ""


class MetadataLogger:
    """
    Records every action/error during the crawl.
    Writes a JSONL log file and provides query methods for the report.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._records: List[ActionRecord] = []

        # Prepare log directory and file
        log_dir = config.OUTPUT_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"{run_id}.jsonl"

    # ── Write API ──────────────────────────────────────────────────────────────

    def log_action(
        self,
        url: str,
        action: str,
        element_label: str,
        element_type: str,
    ) -> None:
        """Record a successful action."""
        record = ActionRecord(
            timestamp=_now(),
            url=url,
            element_type=element_type,
            element_label=element_label,
            action=action,
            success=True,
        )
        self._records.append(record)
        self._append_jsonl(record)

    def log_error(
        self,
        url: str,
        action: str,
        error_type: str,
        error_message: str,
        element_label: str = "",    # optional — not all errors are element-level
        element_type: str = "",     # optional — navigation errors have no element
        screenshot_path: str = "",
    ) -> None:
        """Record a failed action with error details."""
        record = ActionRecord(
            timestamp=_now(),
            url=url,
            element_type=element_type,
            element_label=element_label,
            action=action,
            success=False,
            error_type=error_type,
            error_message=error_message,
            screenshot_path=screenshot_path,
        )
        self._records.append(record)
        self._append_jsonl(record)

    # ── Read API ───────────────────────────────────────────────────────────────

    def get_all_records(self) -> List[ActionRecord]:
        """Return all recorded actions (success + errors)."""
        return list(self._records)

    def get_errors(self) -> List[ActionRecord]:
        """Return only failed actions."""
        return [r for r in self._records if not r.success]

    def get_successes(self) -> List[ActionRecord]:
        """Return only successful actions."""
        return [r for r in self._records if r.success]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _append_jsonl(self, record: ActionRecord) -> None:
        """Append one record to the JSONL log file."""
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to write JSONL log: %s", exc)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
