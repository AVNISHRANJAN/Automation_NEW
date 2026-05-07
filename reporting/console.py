"""
reporting/console.py — Structured, coloured terminal output for Web Auto Tester.

Design principles:
  - ZERO external dependencies (stdlib ANSI only).
  - All user-visible terminal output goes through this module.
  - logger.* calls are for FILE logs only; this module owns the terminal.
  - Keep functions small, single-purpose, and easy to mock in tests.

Colour palette:
  CYAN    → section headers, page headers
  GREEN   → pass / success
  RED     → fail / error
  YELLOW  → warnings / skipped
  MAGENTA → element type badges
  WHITE   → neutral data
  DIM     → separators, tree branches
"""

import sys
from typing import List, Dict

# ── ANSI colour codes ──────────────────────────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_MAGENTA= "\033[35m"
_BLUE   = "\033[34m"
_WHITE  = "\033[97m"

# Disable colours when not writing to a real terminal (CI, file redirect, etc.)
_USE_COLOR = sys.stdout.isatty()


def _c(color: str, text: str) -> str:
    """Wrap text in a colour code, or return plain text if no TTY."""
    if not _USE_COLOR:
        return text
    return f"{color}{text}{_RESET}"


def _bold(text: str) -> str:
    return _c(_BOLD, text)


def _dim(text: str) -> str:
    return _c(_DIM, text)


# ── Width constant ─────────────────────────────────────────────────────────────
_W = 60   # separator width


def _sep(char: str = "═") -> str:
    return _c(_DIM, char * _W)


def _thin_sep() -> str:
    return _c(_DIM, "─" * _W)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def print_banner(target_url: str, run_id: str, headless: bool, max_pages: int) -> None:
    """Print the startup banner."""
    print()
    print(_sep())
    print(_c(_CYAN + _BOLD, f"  {'WEB AUTO TESTER':^{_W - 4}}"))
    print(_sep())
    print(f"  {_c(_BLUE, 'Target URL')} : {_bold(target_url)}")
    print(f"  {_c(_BLUE, 'Run ID    ')} : {run_id}")
    print(f"  {_c(_BLUE, 'Headless  ')} : {headless}")
    print(f"  {_c(_BLUE, 'Max Pages ')} : {max_pages}")
    print(_sep())
    print()


def print_page_header(page_num: int, url: str) -> None:
    """Print the header for each page being tested."""
    print()
    print(_thin_sep())
    label = _c(_CYAN + _BOLD, f"[{page_num}] Testing:")
    print(f"  {label}")
    print(f"  {_c(_WHITE, url)}")
    print()


def print_element_count(total: int) -> None:
    """Print total elements discovered on a page."""
    print(f"  {_c(_BLUE, 'Elements Found')} : {_bold(str(total))}")
    print()


def print_section_header(section: str) -> None:
    """
    Print a section header like:
      FORMS
    """
    print(f"  {_c(_MAGENTA + _BOLD, section)}")


def print_section_summary(items: List[tuple]) -> None:
    """
    Print a tree-style section summary.
    items: list of (label, count, passed) tuples
    Example:
      ├── Checkboxes : 13 ✓
      └── Inputs     : 14 ✓
    """
    for i, (label, count, passed) in enumerate(items):
        is_last  = i == len(items) - 1
        branch   = _dim("└──") if is_last else _dim("├──")
        icon     = _c(_GREEN, "✓") if passed else _c(_RED, "✗")
        padded   = f"{label:<12}"
        print(f"  {branch} {padded} : {count} {icon}")
    print()


def print_action(success: bool, elem_type: str, label: str, action: str) -> None:
    """
    Print a single element interaction result.
      ✓  [BUTTON] Submit → click
      ✗  [INPUT_TEXT] Username → ERROR captured
      ~  [RADIO] Option A → skipped (stale handle)
    """
    label   = label[:50]
    action  = action[:55]

    if action.startswith("skipped"):
        icon     = _c(_YELLOW, "~")
        type_str = _c(_YELLOW, f"[{elem_type}]")
        act_str  = _dim(action)
    elif success:
        icon     = _c(_GREEN, "✓")
        type_str = _c(_GREEN, f"[{elem_type}]")
        act_str  = action
    else:
        icon     = _c(_RED, "✗")
        type_str = _c(_RED, f"[{elem_type}]")
        act_str  = _c(_RED, action)

    print(f"    {icon}  {type_str} {label} {_dim('→')} {act_str}")


def print_form_group_result(
    success: bool,
    group_type: str,   # "CHECKBOX_GROUP" or "RADIO_GROUP"
    group_name: str,
    label: str,
    action: str,
) -> None:
    """Print a form group test result (checkbox/radio)."""
    label  = label[:40]
    icon   = _c(_GREEN, "✓") if success else _c(_RED, "✗")
    color  = _GREEN if success else _RED
    badge  = _c(color, f"[{group_type}:{group_name}]")
    print(f"    {icon}  {badge} {label} {_dim('→')} {action}")


def print_broken_link(url: str, reason: str = "") -> None:
    """Print a broken link warning line."""
    suffix = _dim(f" ({reason})") if reason else ""
    print(f"  {_c(_RED, '✗')} {_c(_RED, 'Broken link')} : {url}{suffix}")


def print_iframe_found(count: int) -> None:
    """Print iframe element discovery notice."""
    print(f"  {_dim('→')} {count} element(s) found in iframes")


def print_links_enqueued(enqueued: int, queue_size: int) -> None:
    """Print crawl queue status after a page completes."""
    print(f"  {_dim('→')} {enqueued} new URL(s) queued  "
          f"{_dim(f'({queue_size} total in queue)')}")


def print_page_summary(passed: int, failed: int, skipped: int) -> None:
    """Print a compact per-page pass/fail/skip summary."""
    print()
    g = _c(_GREEN, f"{passed} passed")
    r = _c(_RED,   f"{failed} failed") if failed else _dim("0 failed")
    y = _c(_YELLOW, f"{skipped} skipped") if skipped else _dim("0 skipped")
    print(f"  {_dim('Summary:')}  {g}  ·  {r}  ·  {y}")


def print_error_block(errors: List[dict]) -> None:
    """
    Print a compact ERRORS section at the bottom of a page.
    errors: list of {'label': str, 'action': str, 'message': str}
    """
    if not errors:
        return
    print()
    print_section_header("ERRORS")
    for i, e in enumerate(errors):
        is_last = i == len(errors) - 1
        branch  = _dim("└──") if is_last else _dim("├──")
        label   = e.get("label", "?")[:40]
        msg     = e.get("message", "")[:60]
        print(f"  {branch} {_c(_RED, label)} {_dim('→')} {msg}")
    print()


def print_manifest_saved(path: str, pages: int, elements: int) -> None:
    """Print manifest save confirmation."""
    print(f"\n  {_c(_GREEN, '[✓]')} Element manifest saved")
    print(f"      {_dim(path)}")
    print(f"      Covers {pages} page(s), {elements} element(s)")


def print_crawl_complete(total_pages: int) -> None:
    """Print hyperlink test completion banner."""
    print()
    print(_sep())
    print(f"  {_c(_GREEN, '✓')}  All hyperlinks tested  ·  "
          f"{_bold(str(total_pages))} unique page(s) visited")
    print(_sep())
    print()


def print_final_summary(
    pages: int,
    total_elements: int,
    passed: int,
    failed: int,
    screenshots: int,
    report_path: str,
    screenshot_dir: str,
    excel_path: str = "",
    inventory_path: str = "",
) -> None:
    """Print the final execution summary banner."""
    status_line = (
        _c(_GREEN + _BOLD, "  ALL TESTS PASSED ✓")
        if failed == 0
        else _c(_RED + _BOLD, f"  {failed} FAILURE(S) DETECTED ✗")
    )

    print()
    print(_sep())
    print(_c(_CYAN + _BOLD, f"  {'FINAL SUMMARY':^{_W - 4}}"))
    print(_sep())
    print(f"  {_c(_BLUE, 'Pages Tested    ')} : {_bold(str(pages))}")
    print(f"  {_c(_BLUE, 'Total Elements  ')} : {_bold(str(total_elements))}")
    print(f"  {_c(_GREEN, 'Passed Actions  ')} : {_bold(str(passed))}")
    print(f"  {_c(_RED  if failed else _DIM, 'Failed Actions  ')} : {_bold(str(failed))}")
    print(f"  {_c(_BLUE, 'Screenshots     ')} : {_bold(str(screenshots))}")
    print(f"  {_c(_BLUE, 'HTML Report     ')} : {report_path}")
    if excel_path:
        print(f"  {_c(_GREEN, 'Excel Report    ')} : {excel_path}")
    if inventory_path:
        print(f"  {_c(_GREEN, 'UI Inventory    ')} : {inventory_path}")
    print(f"  {_c(_BLUE, 'Screenshots Dir ')} : {screenshot_dir}")
    print(_sep())
    print(status_line)
    print(_sep())
    print()



def print_login_required(url: str) -> None:
    """Print the manual login prompt."""
    print()
    print(_sep())
    print(_c(_YELLOW + _BOLD, f"  {'LOGIN REQUIRED':^{_W - 4}}"))
    print(_sep())
    print(f"  URL: {url}")
    print("  Please log in manually in the browser window.")
    print("  Testing will resume automatically after login.")
    print(_sep())
    print()


def print_login_success(url: str) -> None:
    print(f"\n  {_c(_GREEN, '[✓]')} Login successful. Resuming on: {url}\n")


def print_login_timeout() -> None:
    print(f"\n  {_c(_YELLOW, '[!]')} Login timeout. Proceeding without authentication.\n")


def print_nav_failed(url: str) -> None:
    print(f"\n  {_c(_RED, '[✗]')} Failed to load: {url}\n")
