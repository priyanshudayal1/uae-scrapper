import sys
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
from playwright.sync_api import sync_playwright


TARGET_URL = "https://uaelegislation.gov.ae"
DOWNLOAD_DIR = Path("downloads")


def sanitize_filename(name: str) -> str:
    """Sanitize the filename to be filesystem safe."""
    # Remove invalid characters for Windows/Linux
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()[:200]


def scrape_with_playwright(headless: bool = True, max_pdfs: int = 5) -> int:
    """Loads the site with a real browser context and grabs PDF links."""
    print(f"Launching browser to fetch {TARGET_URL} ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        # Increase default timeout for navigation and selectors
        context.set_default_timeout(60000)
        
        page = context.new_page()
        try:
            page.goto(f"{TARGET_URL}/", wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            print("Timed out waiting for the page to load.")
            browser.close()
            return 1

        print("Switching to English...")
        navigate_to_english(page)
        print("Closing modal if present...")
        dismiss_popup(page)

        title = page.title()
        print(f"Page title: {title}")

        # 1. Get Categories
        print("Fetching categories...")
        # Wait for categories to be visible
        try:
            page.locator(".category_listing").wait_for(state="visible", timeout=10000)
        except:
            print("Could not find category listing.")
        
        category_elements = page.locator(".category_listing .item_ a.link_").all()
        categories = []
        for el in category_elements:
            href = el.get_attribute("href")
            # Title is in .content_ p
            text_el = el.locator(".content_ p")
            text = text_el.inner_text().strip() if text_el.count() > 0 else "Unknown"
            if href:
                categories.append((text, href))

        print(f"Found {len(categories)} categories.")

        # Only process the first category
        if not categories:
            print("No categories found. Exiting.")
            browser.close()
            return 1
            
        cat_name, cat_href = categories[0]
        print(f"\nProcessing ONLY First Category: {cat_name}")
        
        if not cat_href or "javascript" in cat_href or cat_href == "#":
            print(f"Invalid href: {cat_href}")
            browser.close()
            return 1

        cat_url = urljoin(TARGET_URL, cat_href)
        print(f"Navigating to: {cat_url}")
        
        # Create directory
        safe_cat_name = sanitize_filename(cat_name)
        cat_dir = DOWNLOAD_DIR / safe_cat_name
        cat_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            page.goto(cat_url, wait_until="domcontentloaded", timeout=60000)
            
            # Check for 404 or Page Not Found
            if "page-not-found" in page.url or "404" in page.title() or "Page not found" in page.title():
                print(f"Category page not found (404): {cat_url}")
                browser.close()
                return 1
                
        except PlaywrightTimeoutError:
            print(f"Timeout loading category: {cat_name}")
            browser.close()
            return 1

        # 2. Tick only Law Type filter (select all law types)
        print("Ticking Law Type filter only...")
        try:
            # Wait for filters to be present
            page.locator("#filter-div").wait_for(state="visible", timeout=10000)
            page.wait_for_timeout(1000)  # Give page time to stabilize
            
            # Only tick Law Type filter
            selector = "input[name='lawtype-all']"
            tname = "Law Type"
            
            try:
                chk = page.locator(selector)
                if chk.count() > 0:
                    # Scroll into view to be safe
                    chk.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    
                    if not chk.is_checked():
                        print(f"Ticking filter: {tname}")
                        
                        # Try multiple methods to click
                        clicked = False
                        
                        # Method 1: Click the label (most reliable)
                        try:
                            for_attr = chk.get_attribute("id")
                            if for_attr:
                                label = page.locator(f"label[for='{for_attr}']")
                                if label.count() > 0:
                                    label.click(force=True)
                                    clicked = True
                                    print(f"  Clicked label for {tname}")
                        except Exception as e:
                            print(f"  Method 1 failed: {e}")
                        
                        # Method 2: Click the span next to input
                        if not clicked:
                            try:
                                span_locator = page.locator(f"{selector} + span")
                                if span_locator.count() > 0:
                                    span_locator.click(force=True)
                                    clicked = True
                                    print(f"  Clicked span for {tname}")
                            except Exception as e:
                                print(f"  Method 2 failed: {e}")
                        
                        # Method 3: Click input directly
                        if not clicked:
                            try:
                                chk.click(force=True)
                                clicked = True
                                print(f"  Clicked input for {tname}")
                            except Exception as e:
                                print(f"  Method 3 failed: {e}")
                        
                        # Wait for loader after clicking
                        if clicked:
                            try:
                                # Wait for loader to appear
                                page.locator(".form_loader").wait_for(state="visible", timeout=3000)
                                # Wait for loader to disappear
                                page.locator(".form_loader").wait_for(state="hidden", timeout=60000)
                            except PlaywrightTimeoutError:
                                # No loader or it's already gone
                                page.wait_for_timeout(2000)
                            
                            # Verify it got checked
                            page.wait_for_timeout(500)
                            if chk.is_checked():
                                print(f"  ✓ {tname} successfully ticked")
                            else:
                                print(f"  ✗ {tname} click didn't work, but continuing...")
                    else:
                        print(f"Filter {tname} is already checked.")
                else:
                    print(f"Filter {tname} not found.")
            except Exception as e:
                print(f"Error ticking {tname}: {e}")

        except Exception as e:
            print(f"Error applying filters: {e}")

        # 3. Pagination Loop
        page_num = 1
        while True:
            print(f"\nScraping page {page_num} of {cat_name}...")
            
            # Wait for table rows
            try:
                page.locator("#legislationsTable").wait_for(state="visible", timeout=15000)
                # Wait a bit for rows to populate if they are dynamic
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"Table not found or timed out: {e}")
            
            rows = page.locator("#legislationsTable .body_tr").all()
            print(f"Found {len(rows)} legislations on this page.")
            
            if not rows:
                # Try alternative selector
                rows = page.locator(".l_t_body .body_tr").all()
                print(f"Trying alternative selector, found {len(rows)} rows.")
                
            if not rows:
                print("No rows found. Stopping pagination for this category.")
                break

            for row in rows:
                try:
                    # Title is in the first 'a' tag inside .body_td
                    title_link = row.locator(".body_td > a").first
                    if title_link.count() == 0:
                        # Try alternative: direct child link
                        title_link = row.locator("a").first
                        
                    if title_link.count() == 0:
                        print("No title link found in row, skipping.")
                        continue
                        
                    title = title_link.inner_text().strip()
                    safe_title = sanitize_filename(title)
                    
                    if not title:
                        print("Empty title, skipping.")
                        continue
                    
                    print(f"\nProcessing: {title[:80]}...")
                    
                    # Find all download links directly - they have href containing /download
                    download_links = []
                    all_download_anchors = row.locator("a[href*='/download']").all()
                    
                    for link in all_download_anchors:
                        try:
                            pdf_href = link.get_attribute("href")
                            lang_text = link.inner_text().strip()
                            
                            if pdf_href and "/download" in pdf_href:
                                # Avoid duplicates
                                if pdf_href not in [dl[0] for dl in download_links]:
                                    download_links.append((pdf_href, lang_text))
                        except Exception as e:
                            print(f"  Error getting download link: {e}")
                            continue
                    
                    print(f"  Found {len(download_links)} download links")
                    
                    if not download_links:
                        print("  No download links found for this legislation.")
                        continue
                    
                    for pdf_href, lang_text in download_links:
                        # Determine filename suffix based on URL path (en or ar)
                        if "/ar/" in pdf_href or lang_text == "ع":
                            suffix = "AR"
                        else:
                            suffix = "EN"
                            
                        filename = f"{safe_title}_{suffix}.pdf"
                        file_path = cat_dir / filename
                        
                        if file_path.exists():
                            print(f"  Skipping {filename} (already exists)")
                            continue
                            
                        # Download
                        abs_pdf_url = urljoin(TARGET_URL, pdf_href)
                        print(f"  Downloading {suffix} version...")
                        
                        try:
                            # We use the browser context to download to share cookies
                            response = context.request.get(abs_pdf_url, timeout=60000)
                            if response.ok:
                                content = response.body()
                                # Check if response is actually a PDF (not an HTML error page)
                                if len(content) > 0 and (content[:4] == b'%PDF' or len(content) > 1000):
                                    file_path.write_bytes(content)
                                    print(f"  ✓ Saved {filename}")
                                else:
                                    print(f"  ✗ Skipping {filename}: Not a valid PDF (possibly auth required or error page)")
                            else:
                                print(f"  ✗ Failed to download: HTTP {response.status}")
                        except PlaywrightError as e:
                            print(f"  ✗ Playwright error: {e}")
                            if "Target page, context or browser has been closed" in str(e):
                                print("Browser closed unexpectedly. Aborting.")
                                return 1
                        except Exception as e:
                            print(f"  ✗ Error downloading: {e}")
                            
                        # Add delay to be polite and avoid crashes
                        page.wait_for_timeout(500)
                            
                except Exception as e:
                    print(f"Error processing row: {e}")

            # Check for Next Page
            next_btn = page.locator("#legislationsPaginator a.next_")
            
            if next_btn.count() == 0:
                # Try alternative selector
                next_btn = page.locator(".table_pagination a.next_")
            
            if next_btn.count() > 0 and next_btn.is_visible():
                next_href = next_btn.get_attribute("href")
                # Check if href is valid
                if next_href and next_href != "#" and "javascript" not in next_href:
                    print(f"\nNavigating to page {page_num + 1}...")
                    try:
                        # Click the next button instead of goto for AJAX pagination
                        next_btn.click()
                        
                        # Wait for either URL change or table content change
                        page.wait_for_timeout(2000)
                        
                        # Wait for table to update (loader to disappear if any)
                        try:
                            page.locator(".form_loader").wait_for(state="hidden", timeout=10000)
                        except:
                            pass
                        
                        # Wait for new content
                        page.locator("#legislationsTable").wait_for(state="visible", timeout=15000)
                        page.wait_for_timeout(1500)
                        
                        page_num += 1
                    except Exception as e:
                        print(f"Error navigating to next page: {e}")
                        # Try direct navigation as fallback
                        try:
                            full_url = urljoin(TARGET_URL, next_href)
                            page.goto(full_url, wait_until="domcontentloaded", timeout=60000)
                            page.wait_for_timeout(2000)
                            page_num += 1
                        except Exception as e2:
                            print(f"Fallback navigation also failed: {e2}")
                            break
                else:
                    print("\nNo more pages (href is empty or #).")
                    break
            else:
                print("\nNo next page button found.")
                break

        browser.close()
        return 0


def dismiss_popup(page) -> None:
	"""Close the fancybox modal if present."""
	close_btn = page.locator("[data-fancybox-close]")
	try:
		if close_btn.is_visible(timeout=5000):
			close_btn.click(force=True)
			page.wait_for_timeout(500)
			return
	except PlaywrightTimeoutError:
		pass
	page.keyboard.press("Escape")
	page.wait_for_timeout(300)


def navigate_to_english(page) -> None:
	"""Navigate to English version; fall back to clicking language toggles."""
	try:
		page.goto(f"{TARGET_URL}/en", wait_until="domcontentloaded", timeout=20000)
		return
	except PlaywrightTimeoutError:
		pass

	lang_toggle = page.locator("text=English")
	# alt_toggle was too broad, causing strict mode errors.
	# We rely on text=English or the direct URL navigation.
	
	try:
		if lang_toggle.is_visible(timeout=4000):
			lang_toggle.click(force=True)
			page.wait_for_timeout(800)
			return
	except PlaywrightTimeoutError:
		pass


def main() -> int:
	return scrape_with_playwright(headless=False, max_pdfs=5)


if __name__ == "__main__":
	sys.exit(main())
