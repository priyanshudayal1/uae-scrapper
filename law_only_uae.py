"""
UAE Legislation PDF Scraper
---
Goes to https://uaelegislation.gov.ae/en/legislations
Applies Year filter (all years), paginates through every page,
downloads EN PDFs, uploads to S3 (uae-bareacts), deletes local copy.
Tracks progress in crawler_state.json for resume support.
"""

import sys
import re
import os
import json
import random
import boto3
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin
from dotenv import load_dotenv

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
    sync_playwright,
)

load_dotenv()

# ----------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------
TARGET_URL = "https://uaelegislation.gov.ae"
LEGISLATIONS_URL = f"{TARGET_URL}/en/legislations"
DOWNLOAD_DIR = Path("downloads/legislations")
STATE_FILE = "crawler_state.json"
S3_BUCKET = "uae-bareacts"


# ================================================================
# STATE TRACKER  (JSON-based, supports resume)
# ================================================================
class CrawlerState:
    """Tracks downloaded legislation IDs so we never re-download."""

    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.data = {
            "last_updated": None,
            "last_page": 0,
            "total_downloaded": 0,
            "total_failed": 0,
            "downloaded": {},   # leg_id -> metadata dict
            "failed": {},       # leg_id -> metadata dict
        }
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                print(
                    f"Loaded state: {len(self.data['downloaded'])} downloaded, "
                    f"{len(self.data['failed'])} failed, last page {self.data['last_page']}"
                )
            except Exception as e:
                print(f"Error loading state: {e}. Starting fresh.")

    def save(self):
        self.data["last_updated"] = datetime.now().isoformat()
        self.data["total_downloaded"] = len(self.data["downloaded"])
        self.data["total_failed"] = len(self.data["failed"])
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"Error saving state: {e}")

    def is_downloaded(self, leg_id: str) -> bool:
        return str(leg_id) in self.data["downloaded"]

    def mark_downloaded(
        self, leg_id, title, year, number, en_s3_key=None
    ):
        self.data["downloaded"][str(leg_id)] = {
            "title": title,
            "year": year,
            "number": number,
            "en_s3_key": en_s3_key,
            "timestamp": datetime.now().isoformat(),
        }
        self.data["failed"].pop(str(leg_id), None)
        self.save()

    def mark_failed(self, leg_id, title, error):
        self.data["failed"][str(leg_id)] = {
            "title": title,
            "error": error,
            "timestamp": datetime.now().isoformat(),
        }
        self.save()

    def set_last_page(self, page_num: int):
        self.data["last_page"] = page_num
        self.save()

    def get_last_page(self) -> int:
        return self.data.get("last_page", 0)


# ================================================================
# S3 HELPERS
# ================================================================
_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
    return _s3_client


def upload_to_s3(local_path: Path, s3_key: str) -> bool:
    """Upload file to S3 and delete local copy on success."""
    try:
        s3_key = s3_key.replace("\\", "/")
        _get_s3().upload_file(str(local_path), S3_BUCKET, s3_key)
        print(f"      Uploaded -> s3://{S3_BUCKET}/{s3_key}")
        if local_path.exists():
            local_path.unlink()
            print(f"      Deleted local: {local_path.name}")
        return True
    except Exception as e:
        print(f"      S3 upload failed: {e}")
        return False


# ================================================================
# ANTI-BOT / BROWSER HELPERS
# ================================================================
def _build_proxy_config():
    server = os.getenv("UAE_PROXY_SERVER", "").strip()
    if not server:
        return None
    proxy = {"server": server}
    u = os.getenv("UAE_PROXY_USERNAME", "").strip()
    pw = os.getenv("UAE_PROXY_PASSWORD", "").strip()
    if u:
        proxy["username"] = u
    if pw:
        proxy["password"] = pw
    return proxy


def _launch_browser(p, headless: bool):
    """Launch Firefox with stealth prefs."""
    opts = {
        "headless": headless,
        "firefox_user_prefs": {
            "dom.webdriver.enabled": False,
            "useAutomationExtension": False,
            "media.peerconnection.enabled": False,
            "privacy.resistFingerprinting": False,
            "general.platform.override": "Win64",
        },
    }
    proxy = _build_proxy_config()
    if proxy:
        opts["proxy"] = proxy
        print(f"Using proxy: {proxy['server']}")
    return p.firefox.launch(**opts)


def _is_cloudflare_block(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    try:
        body = (page.locator("body").inner_text(timeout=4000) or "").lower()
    except Exception:
        body = ""
    markers = [
        "attention required",
        "cloudflare",
        "sorry, you have been blocked",
        "you are unable to access",
    ]
    combined = f"{title}\n{body}"
    return any(m in combined for m in markers)


def _apply_anti_bot(context):
    context.set_extra_http_headers(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
    )
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });
        const origQuery = window.Permissions?.prototype?.query;
        if (origQuery) {
            window.Permissions.prototype.query = (params) =>
                params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params);
        }
        """
    )


def _delay(page, lo=800, hi=2200):
    page.wait_for_timeout(random.randint(lo, hi))


def _humanize(page):
    _delay(page, 1500, 3000)
    for _ in range(random.randint(2, 4)):
        page.mouse.move(random.randint(100, 900), random.randint(150, 500))
        _delay(page, 200, 500)
    page.mouse.wheel(0, random.randint(200, 600))
    _delay(page, 1000, 2500)
    page.mouse.wheel(0, -random.randint(50, 200))
    _delay(page, 500, 1000)


def _recover_cloudflare(page, max_attempts=5) -> bool:
    print("  Cloudflare block detected - attempting recovery ...")
    for attempt in range(1, max_attempts + 1):
        wait = 5 + attempt * 4
        print(f"    Attempt {attempt}/{max_attempts} (wait {wait}s) ...")
        page.wait_for_timeout(wait * 1000)
        page.mouse.move(random.randint(100, 700), random.randint(200, 500))
        page.mouse.move(random.randint(300, 800), random.randint(100, 400))
        if not _is_cloudflare_block(page):
            print("    Challenge cleared.")
            return True
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except PlaywrightTimeoutError:
            pass
        if not _is_cloudflare_block(page):
            print("    Cleared after reload.")
            return True
    return False


def _dismiss_popup(page):
    close = page.locator("[data-fancybox-close]").first
    try:
        if close.is_visible(timeout=5000):
            close.click(force=True)
            page.wait_for_timeout(500)
            return
    except PlaywrightTimeoutError:
        pass
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()[:200]


# ================================================================
# DOWNLOAD A SINGLE PDF (via direct download URL)
# ================================================================
def _download_pdf(context, leg_id, local_path: Path, s3_key: str):
    """Navigate directly to /en/legislations/{id}/download to grab the PDF.
    Returns the S3 key string on success, or None on failure.
    """
    download_url = f"{TARGET_URL}/en/legislations/{leg_id}/download"
    dl_page = context.new_page()

    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Navigate to the download URL.
        # Firefox treats this as a file download, so page.goto() throws
        # "Download is starting" — that's the SUCCESS case.
        downloaded = False
        try:
            with dl_page.expect_download(timeout=60000) as dl_info:
                try:
                    dl_page.goto(download_url, wait_until="commit", timeout=60000)
                except PlaywrightError as nav_err:
                    # "Download is starting" means the download was triggered — good!
                    if "download" not in str(nav_err).lower():
                        raise
            download = dl_info.value
            download.save_as(str(local_path))
            downloaded = True
        except PlaywrightTimeoutError:
            pass

        # Fallback: use context API request with Referer
        if not downloaded:
            print("      Browser download failed, trying API request ...")
            referer = f"{TARGET_URL}/en/legislations/{leg_id}"
            resp = context.request.get(
                download_url,
                headers={"Referer": referer, "Accept": "application/pdf,*/*"},
                timeout=60000,
            )
            if not resp.ok:
                print(f"      HTTP {resp.status}")
                return None
            body = resp.body()
            if len(body) < 500:
                print(f"      Too small ({len(body)} bytes)")
                return None
            local_path.write_bytes(body)

        # Validate
        if not local_path.exists():
            return None
        size = local_path.stat().st_size
        if size < 500:
            local_path.unlink(missing_ok=True)
            print(f"      File too small ({size} bytes), not a valid PDF")
            return None

        print(f"      Saved {local_path.name}  ({size / 1024:.0f} KB)")

        if upload_to_s3(local_path, s3_key):
            return s3_key
        return f"local:{local_path}"

    except PlaywrightError as e:
        if "closed" in str(e).lower():
            raise
        print(f"      Download error: {e}")
        return None
    except Exception as e:
        print(f"      Download error: {e}")
        return None
    finally:
        try:
            dl_page.close()
        except Exception:
            pass


# ================================================================
# LOADER + TABLE WAITS
# ================================================================
def _wait_for_loader(page, timeout=15000):
    """Wait for the loading spinner (.l_) to disappear if present."""
    try:
        loader = page.locator(".l_")
        if loader.count() > 0 and loader.first.is_visible():
            loader.first.wait_for(state="hidden", timeout=timeout)
    except Exception:
        pass


def _wait_for_table(page, timeout=20000):
    """Wait until #legislationsTable is visible and rows are loaded."""
    try:
        page.locator("#legislationsTable").wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        print("  Table not visible yet, extra wait ...")
        page.wait_for_timeout(5000)

    # Wait for loader overlay to disappear
    _wait_for_loader(page)

    # Stabilisation wait
    page.wait_for_timeout(2000)


# ================================================================
# APPLY YEAR FILTER
# ================================================================
def _apply_year_filter(page):
    """Tick the Year 'select-all' checkbox if not already checked."""
    print("\nApplying Year filter (all years) ...")
    try:
        page.locator("[data-filter-item]").first.wait_for(
            state="visible", timeout=15000
        )
        _delay(page, 500, 1000)

        # The select-all for Year has name='year-all'
        chk = page.locator("input[name='year-all']")
        if chk.count() == 0:
            print("  Year select-all checkbox not found - continuing anyway.")
            return

        if chk.is_checked():
            print("  Already checked.")
            return

        # Try clicking the label, then span sibling, then input
        clicked = False
        for method, selector in [
            ("label", "label[for='year']"),
            ("span", "input[name='year-all'] + span"),
        ]:
            try:
                el = page.locator(selector)
                if el.count() > 0:
                    el.first.click(force=True)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            chk.click(force=True)

        # Wait for loader + table refresh
        try:
            loader = page.locator(".l_")
            loader.first.wait_for(state="visible", timeout=3000)
            loader.first.wait_for(state="hidden", timeout=60000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(3000)
        except Exception:
            page.wait_for_timeout(3000)

        if chk.is_checked():
            print("  Year filter applied.")
        else:
            print("  Year checkbox didn't stick, but continuing ...")

    except Exception as e:
        print(f"  Error applying year filter: {e}")


# ================================================================
# PARSE ONE PAGE OF LEGISLATION ROWS
# ================================================================
def _parse_rows(page):
    """Return list of dicts with leg_id, title, number, year."""
    rows = page.locator("#legislationsTable .body_tr").all()
    entries = []

    for row in rows:
        try:
            # Title link
            tl = row.locator(".body_td > a").first
            if tl.count() == 0:
                tl = row.locator("a").first
            if tl.count() == 0:
                continue

            title = tl.inner_text().strip()
            href = tl.get_attribute("href") or ""
            if not title or not href:
                continue

            m = re.search(r"/legislations/(\d+)", href)
            if not m:
                continue
            leg_id = m.group(1)

            # Number & Year from span.text_center
            spans = row.locator("span.text_center").all()
            number = spans[0].inner_text().strip() if len(spans) > 0 else ""
            year = spans[1].inner_text().strip() if len(spans) > 1 else ""

            entries.append(
                {
                    "leg_id": leg_id,
                    "title": title,
                    "number": number,
                    "year": year,
                }
            )
        except Exception as e:
            print(f"  Row parse error: {e}")
            continue

    return entries


# ================================================================
# MAIN SCRAPER
# ================================================================
def scrape_legislations(headless=True, resume=True, weekly_mode=False):
    """
    Main entry point.

    headless    : run browser in headless mode
    resume      : skip already-downloaded items
    weekly_mode : stop early when hitting already-downloaded pages
    """
    state = CrawlerState()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("UAE Legislation PDF Scraper")
    print("=" * 60)
    print(f"  Target URL  : {LEGISLATIONS_URL}")
    print(f"  S3 bucket   : {S3_BUCKET}")
    print(f"  Local dir   : {DOWNLOAD_DIR.absolute()}")
    print(f"  State file  : {STATE_FILE}")
    print(f"  Already done: {len(state.data['downloaded'])}")
    print(f"  Weekly mode : {weekly_mode}")
    print()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
                "Gecko/20100101 Firefox/124.0"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Dubai",
            color_scheme="light",
        )
        _apply_anti_bot(context)
        context.set_default_timeout(60000)
        page = context.new_page()

        # -- Initial navigation --
        print("Navigating to legislations page ...")
        for nav_try in range(1, 4):
            try:
                page.goto(
                    LEGISLATIONS_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                break
            except PlaywrightTimeoutError:
                if nav_try == 3:
                    print("Timed out after 3 attempts.")
                    browser.close()
                    return 1
                print(f"  Attempt {nav_try} timed out, retrying ...")
                _delay(page, 3000, 5000)

        _humanize(page)

        if _is_cloudflare_block(page):
            if not _recover_cloudflare(page):
                print("Blocked by Cloudflare. Try a proxy or different IP.")
                browser.close()
                return 1

        _dismiss_popup(page)
        _wait_for_loader(page)
        print(f"Page title: {page.title()}")

        # -- Apply Year filter --
        _apply_year_filter(page)
        _wait_for_loader(page)
        _delay(page, 1500, 2500)

        # -- Pagination loop --
        page_num = 1
        total_dl = 0
        total_skip = 0
        total_fail = 0
        consecutive_all_skipped_pages = 0

        while True:
            print(f"\n{'='*60}")
            print(f"  PAGE {page_num}")
            print(f"{'='*60}")

            _wait_for_table(page)

            if _is_cloudflare_block(page):
                if not _recover_cloudflare(page):
                    print("Blocked mid-pagination. Saving state and exiting.")
                    state.set_last_page(page_num)
                    browser.close()
                    return 1

            entries = _parse_rows(page)
            print(f"  Found {len(entries)} legislations")

            if not entries:
                print("  No rows found - stopping.")
                break

            page_dl = 0
            page_skip = 0

            for idx, entry in enumerate(entries, 1):
                lid = entry["leg_id"]
                title = entry["title"]
                year = entry["year"]
                number = entry["number"]
                short = f"{title[:65]}..." if len(title) > 65 else title

                print(f"\n  [{idx}/{len(entries)}] {short}  (No.{number}, {year})")

                # -- Skip if already done --
                if resume and state.is_downloaded(lid):
                    print(f"    Already downloaded (ID {lid}), skipping.")
                    total_skip += 1
                    page_skip += 1
                    continue

                safe = _sanitize(title)
                en_fname = f"{safe}.pdf"
                en_path = DOWNLOAD_DIR / en_fname
                en_s3 = f"legislation/UAE/{en_fname}"

                # -- Download EN PDF (opens detail page in new tab) --
                print("    Downloading EN ...")
                en_result = _download_pdf(
                    context, lid, en_path, en_s3
                )

                # -- Record outcome --
                if en_result:
                    state.mark_downloaded(lid, title, year, number, en_result)
                    total_dl += 1
                    page_dl += 1
                    print(f"    Done (ID {lid})")
                else:
                    state.mark_failed(lid, title, "EN download failed")
                    total_fail += 1
                    print(f"    FAILED (ID {lid})")

                _delay(page, 700, 1800)

            # -- Page summary --
            print(
                f"\n  Page {page_num} done: "
                f"+{page_dl} downloaded, {page_skip} skipped"
            )
            state.set_last_page(page_num)

            # -- Weekly mode: stop if all items already downloaded --
            if weekly_mode:
                if page_skip == len(entries):
                    consecutive_all_skipped_pages += 1
                    print(
                        f"  [Weekly] All items on page already downloaded "
                        f"({consecutive_all_skipped_pages} consecutive pages)."
                    )
                    if consecutive_all_skipped_pages >= 2:
                        print("  [Weekly] Stopping - caught up with new legislation.")
                        break
                else:
                    consecutive_all_skipped_pages = 0

            # -- Navigate to next page --
            next_btn = page.locator("#legislationsPaginator a.next_")
            if next_btn.count() == 0:
                next_btn = page.locator(".table_pagination a.next_")

            if next_btn.count() > 0 and next_btn.is_visible():
                next_href = next_btn.get_attribute("href")
                if (
                    next_href
                    and next_href != "#"
                    and "javascript" not in next_href
                ):
                    print(f"\n  Navigating to page {page_num + 1} ...")
                    _delay(page, 1500, 3000)
                    try:
                        next_btn.click()
                        page.wait_for_timeout(2000)
                        _wait_for_loader(page)
                        _wait_for_table(page)
                        page_num += 1
                    except Exception as e:
                        print(f"  Pagination click failed: {e}")
                        try:
                            full_url = urljoin(TARGET_URL, next_href)
                            page.goto(
                                full_url,
                                wait_until="domcontentloaded",
                                timeout=60000,
                            )
                            _wait_for_table(page)
                            page_num += 1
                        except Exception as e2:
                            print(f"  Fallback nav failed: {e2}. Stopping.")
                            break
                else:
                    print("\n  Last page reached.")
                    break
            else:
                print("\n  No next-page button. All pages scraped.")
                break

        browser.close()

    # -- Final summary --
    print(f"\n{'='*60}")
    print("SCRAPING COMPLETE")
    print(f"  Downloaded : {total_dl}")
    print(f"  Skipped    : {total_skip}")
    print(f"  Failed     : {total_fail}")
    print(f"  Total done : {len(state.data['downloaded'])}")
    print(f"{'='*60}")
    return 0


# ================================================================
# CLI
# ================================================================
def main() -> int:
    return scrape_legislations(headless=False, resume=True, weekly_mode=False)


if __name__ == "__main__":
    sys.exit(main())
