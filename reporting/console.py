"""
reporting/console.py — Professional CLI output for the Web Auto Tester.
"""

import os
import sys

# ── ANSI Colour Constants ──────────────────────────────────────────────────
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

_W = 76  # Line width

def _c(color, text):
    return color + str(text) + _RESET

def _bold(text):
    return _BOLD + str(text) + _RESET

def _dim(text):
    return _DIM + str(text) + _RESET


def print_banner(target_url, run_id, headless, max_pages):
    print("")
    print("=" * _W)
    print(_c(_CYAN + _BOLD, "  WEB AUTO TESTER".center(_W - 4)))
    print("=" * _W)
    print("  " + _c(_BLUE, "Target URL") + " : " + _bold(target_url))
    print("  " + _c(_BLUE, "Run ID    ") + " : " + str(run_id))
    print("  " + _c(_BLUE, "Headless  ") + " : " + str(headless))
    print("  " + _c(_BLUE, "Max Pages ") + " : " + str(max_pages))
    print("-" * _W)


def print_page_header(page_num, url):
    print("")
    label = _c(_CYAN + _BOLD, "[" + str(page_num) + "] Testing:")
    print("  " + label)
    print("  " + _c(_WHITE, url))


def print_discovery_summary(total):
    print("  " + _c(_BLUE, "Elements Found") + " : " + _bold(str(total)))


def print_section(section):
    print("")
    print("  " + _c(_MAGENTA + _BOLD, section))


def print_interaction_row(elem_type, label, action, success, reason=None, is_last=False):
    branch = "└──" if is_last else "├──"
    icon   = _c(_GREEN, "✓") if success else _c(_RED, "✗")
    
    if success:
        type_str = _c(_GREEN, "[" + str(elem_type) + "]")
    else:
        type_str = _c(_RED, "[" + str(elem_type) + "]")
        
    act_str = str(action)
    if reason:
        act_str += _dim(" (" + str(reason) + ")")
        
    print("    " + icon + "  " + type_str + " " + str(label) + " " + _dim("→") + " " + act_str)


def print_group_row(group_type, group_name, label, action, success, is_last=False):
    branch = "└──" if is_last else "├──"
    icon   = _c(_GREEN, "✓") if success else _c(_RED, "✗")
    color  = _MAGENTA if success else _RED
    
    badge  = _c(color, "[" + str(group_type) + ":" + str(group_name) + "]")
    print("    " + icon + "  " + badge + " " + str(label) + " " + _dim("→") + " " + str(action))


def print_broken_link(url, reason=None):
    suffix = _dim(" (" + str(reason) + ")") if reason else ""
    print("  " + _c(_RED, "✗") + " " + _c(_RED, "Broken link") + " : " + str(url) + suffix)


def print_iframe_discovery(count):
    print("  " + _dim("→") + " " + str(count) + " element(s) found in iframes")


def print_queue_update(enqueued, queue_size):
    print("  " + _dim("→") + " " + str(enqueued) + " new URL(s) queued  " +
          _dim("(" + str(queue_size) + " total in queue)"))


def print_page_summary(passed, failed, skipped):
    g = _c(_GREEN, str(passed) + " passed")
    r = _c(_RED,   str(failed) + " failed") if failed else _dim("0 failed")
    y = _c(_YELLOW, str(skipped) + " skipped") if skipped else _dim("0 skipped")
    print("  " + _dim("Summary:") + "  " + g + "  ·  " + r + "  ·  " + y)


def print_error(label, msg, is_last=False):
    branch = "└──" if is_last else "├──"
    print("  " + branch + " " + _c(_RED, label) + " " + _dim("→") + " " + str(msg))


def print_inventory_saved(path, pages, elements):
    print("")
    print("  " + _c(_GREEN, "[✓]") + " Element manifest saved")
    print("      " + _dim(path))
    print("      Covers " + str(pages) + " page(s), " + str(elements) + " element(s)")


def print_completion(total_pages):
    print("-" * _W)
    print("  " + _c(_GREEN, "✓") + "  All hyperlinks tested  ·  " +
          _bold(str(total_pages)) + " unique page(s) visited")


def print_final_summary(pages, total_elements, passed, failed, screenshots, report_path, excel_path=None, inventory_path=None, security_findings=0, screenshot_dir=""):
    print("")
    status = _c(_GREEN + _BOLD, "  CRAWL COMPLETED SUCCESSFULLY ✓") if not failed \
        else _c(_RED + _BOLD, "  " + str(failed) + " FAILURE(S) DETECTED ✗")
    
    print("=" * _W)
    print(_c(_CYAN + _BOLD, "  FINAL SUMMARY".center(_W - 4)))
    print("=" * _W)
    print("  " + _c(_BLUE, "Pages Tested    ") + " : " + _bold(str(pages)))
    print("  " + _c(_BLUE, "Total Elements  ") + " : " + _bold(str(total_elements)))
    print("  " + _c(_GREEN, "Passed Actions  ") + " : " + _bold(str(passed)))
    print("  " + _c(_RED  if failed else _DIM, "Failed Actions  ") + " : " + _bold(str(failed)))
    print("  " + _c(_BLUE, "Screenshots     ") + " : " + _bold(str(screenshots)))
    print("  " + _c(_BLUE, "HTML Report     ") + " : " + str(report_path))
    if excel_path:
        print("  " + _c(_GREEN, "Excel Report    ") + " : " + str(excel_path))
    if inventory_path:
        print("  " + _c(_GREEN, "UI Inventory    ") + " : " + str(inventory_path))
    print("  " + _c(_YELLOW, "Security Issues ") + " : " + _bold(str(security_findings)))
    print("  " + _c(_BLUE, "Screenshots Dir ") + " : " + str(screenshot_dir))
    print("=" * _W)
    print(status)
    print("=" * _W)
    print("")


def print_login_banner(url):
    print("")
    print(_c(_YELLOW + _BOLD, "  LOGIN REQUIRED".center(_W - 4)))
    print("-" * _W)
    print("  URL: " + str(url))
    print("  Wait for manual login (timeout 120s)...")


def print_login_success(url):
    print("")
    print("  " + _c(_GREEN, "[✓]") + " Login successful. Resuming on: " + str(url))
    print("")


def print_login_timeout():
    print("")
    print("  " + _c(_YELLOW, "[!]") + " Login timeout. Proceeding without authentication.")
    print("")


def print_load_error(url):
    print("")
    print("  " + _c(_RED, "[✗]") + " Failed to load: " + str(url))
    print("")
