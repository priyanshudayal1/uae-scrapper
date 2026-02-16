"""
UAE Legislation – Weekly Crawler
---
Run this weekly (e.g. via Task Scheduler / cron).
It uses the same state file (crawler_state.json) to detect
which legislations are new, downloads only those, and stops
when it hits 2 consecutive pages of already-downloaded items.

Usage:
    python law_weekly_crawler.py              # headless (default)
    python law_weekly_crawler.py --visible    # show browser window
"""

import sys
import argparse

# Reuse everything from the main scraper
from law_only_uae import scrape_legislations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weekly incremental crawler for UAE Legislation PDFs."
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Run with visible browser instead of headless.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  UAE Legislation – WEEKLY INCREMENTAL CRAWLER")
    print("=" * 60)
    print("  Running in weekly_mode: stops when caught up.\n")

    return scrape_legislations(
        headless=not args.visible,
        resume=True,
        weekly_mode=True,
    )


if __name__ == "__main__":
    sys.exit(main())
