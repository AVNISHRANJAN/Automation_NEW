"""
main.py — Entry point for Web Auto Tester.

Usage:
    python main.py --url https://target-site.com
    python main.py --url https://target-site.com --headless --max-pages 50

Orchestration order (matches the flowchart exactly):
  1. Parse CLI args
  2. Set run_id, init output dirs
  3. Launch browser
  4. Navigate to target URL
  5. Detect login page → wait for manual login if needed
  6. Init crawler state
  7. Run BFS crawler loop (all page + element testing happens inside)
  8. Generate final HTML report
  9. Print summary
"""

import argparse
import asyncio
import logging
import sys
import os
from datetime import datetime, timezone

# ── Project root on path ───────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# Apply CLI overrides BEFORE importing modules that read config at import time
def _apply_cli_overrides(args: argparse.Namespace) -> None:
    if args.headless:
        config.HEADLESS = True
    if args.max_pages:
        config.MAX_PAGES = args.max_pages
    if args.timeout:
        config.ACTION_TIMEOUT = args.timeout * 1000
        config.NAV_TIMEOUT    = args.timeout * 1000

from core.browser import BrowserManager
from core.login_detector import LoginDetector
from core.crawler import Crawler
from reporting.screenshot_manager import ScreenshotManager
from reporting.metadata_logger import MetadataLogger
from reporting.report_builder import ReportBuilder

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Intelligent end-to-end web automation tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --url https://example.com
  python main.py --url https://app.example.com --headless --max-pages 50
  python main.py --url https://example.com --timeout 20
        """
    )
    parser.add_argument(
        "--url", required=True,
        help="Target website URL (e.g. https://example.com)"
    )
    parser.add_argument(
        "--headless", action="store_true", default=False,
        help="Run browser in headless mode (disables manual login)"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help=f"Max pages to crawl (default: {config.MAX_PAGES})"
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="Element/navigation timeout in seconds (default: 10)"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    _apply_cli_overrides(args)

    target_url = args.url.rstrip("/")
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    # Unique run ID for this execution
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print("\n" + "=" * 60)
    print("  WEB AUTO TESTER")
    print("=" * 60)
    print(f"  Target:    {target_url}")
    print(f"  Run ID:    {run_id}")
    print(f"  Headless:  {config.HEADLESS}")
    print(f"  Max pages: {config.MAX_PAGES}")
    print("=" * 60 + "\n")

    # ── Init reporting infrastructure ─────────────────────────────────────────
    screenshot_manager = ScreenshotManager(run_id)
    metadata_logger    = MetadataLogger(run_id)
    report_builder     = ReportBuilder(run_id, target_url)

    # ── Launch browser ─────────────────────────────────────────────────────────
    async with BrowserManager() as bm:
        page = await bm.new_page()

        # ── Step 1: Navigate to target URL ─────────────────────────────────────
        logger.info("Navigating to %s", target_url)
        success = await bm.navigate(page, target_url)
        if not success:
            print(f"\n[✗] Failed to load {target_url}. Exiting.\n")
            sys.exit(1)

        # ── Step 2: Login detection ────────────────────────────────────────────
        detector = LoginDetector(page)
        if await detector.is_login_page():
            if config.HEADLESS:
                print("\n[!] Login page detected in headless mode.")
                print("    Set --headless=false to allow manual login.\n")
            else:
                login_url = page.url
                await detector.wait_for_manual_login(login_url)
        else:
            logger.info("No login page detected. Proceeding.")

        # ── Step 3: Confirm home page is loaded ────────────────────────────────
        home_url = page.url
        logger.info("Home page confirmed: %s", home_url)

        # ── Step 4: Init and run BFS crawler ───────────────────────────────────
        crawler = Crawler(
            page=page,
            start_url=home_url,
            screenshot_manager=screenshot_manager,
            metadata_logger=metadata_logger,
        )

        visited_pages = await crawler.run()

        # ── Step 5: Generate report ────────────────────────────────────────────
        all_records  = metadata_logger.get_all_records()
        errors       = metadata_logger.get_errors()
        report_path  = report_builder.build(all_records, visited_pages)

        # ── Final summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  TEST COMPLETE")
        print("=" * 60)
        print(f"  Pages tested:     {len(visited_pages)}")
        print(f"  Actions logged:   {len(all_records)}")
        print(f"  Errors captured:  {len(errors)}")
        print(f"  Report:           {report_path}")
        print(f"  Screenshots:      {config.SCREENSHOT_DIR / run_id}")
        print("=" * 60 + "\n")

        if errors:
            print(f"  [!] {len(errors)} errors found. Check the report for details.\n")
        else:
            print("  [✓] No errors detected.\n")


if __name__ == "__main__":
    asyncio.run(main())
