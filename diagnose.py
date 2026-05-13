#!/usr/bin/env python3
"""
Diagnostic script to check for import and syntax errors.
"""

import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

print("=" * 60)
print("DIAGNOSTIC: Checking imports and syntax")
print("=" * 60)

modules_to_check = [
    "config",
    "core.element_finder",
    "core.interactor",
    "core.form_tester",
    "core.security_scanner",
    "core.state_tracker",
    "core.browser",
    "core.crawler",
    "reporting.screenshot_manager",
    "reporting.metadata_logger",
    "reporting.console",
]

failed_imports = []
successful_imports = []

for module_name in modules_to_check:
    try:
        print(f"\nTrying to import: {module_name}...", end=" ")
        __import__(module_name)
        print("✓ SUCCESS")
        successful_imports.append(module_name)
    except Exception as e:
        print(f"✗ FAILED")
        print(f"  Error: {type(e).__name__}: {e}")
        failed_imports.append((module_name, e))

print("\n" + "=" * 60)
print(f"Summary: {len(successful_imports)} passed, {len(failed_imports)} failed")
print("=" * 60)

if failed_imports:
    print("\nFailed imports:")
    for module_name, error in failed_imports:
        print(f"  - {module_name}")
        print(f"    {type(error).__name__}: {error}")
    sys.exit(1)
else:
    print("\nAll imports successful!")
    sys.exit(0)
