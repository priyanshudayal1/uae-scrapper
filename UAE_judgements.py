import os
import time
import re
import boto3
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from scraper_tracker import ScraperTracker

# Load environment variables
load_dotenv()

class DIFCCourtsScraper:
    def __init__(self):
        self.base_url = "https://www.difccourts.ae"
        self.start_url = "https://www.difccourts.ae/rules-decisions/judgments-orders"
        
        # Initialize S3
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_REGION')
        )
        self.bucket_name = "uae-judgements"

        # Initialize tracker
        self.tracker = ScraperTracker()
        
        # Create folders for saving files
        self.judgments_folder = "judgments"
        self.orders_folder = "orders"
        os.makedirs(self.judgments_folder, exist_ok=True)
        os.makedirs(self.orders_folder, exist_ok=True)
        
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        # Print the download folders
        print(f"PDFs will be saved to:")
        print(f"  Judgments: {os.path.abspath(self.judgments_folder)}")
        print(f"  Orders: {os.path.abspath(self.orders_folder)}")
        
        self._init_browser()
    
    def _init_browser(self):
        """Initialize or reinitialize the Playwright browser"""
        self._close_browser()
        
        try:
            self.playwright = sync_playwright().start()
            # Headless is required for page.pdf() functionality
            # Added args for Linux/Container robustness
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-extensions'
                ]
            )
            self.context = self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
            )
            self.context.set_default_timeout(30000)
            self.page = self.context.new_page()
            print("Browser session initialized")
        except Exception as e:
            print(f"Failed to initialize browser: {e}")
            raise e

    def _close_browser(self):
        """Clean up browser resources"""
        if self.page:
            try: self.page.close()
            except: pass
        if self.context:
            try: self.context.close()
            except: pass
        if self.browser:
            try: self.browser.close()
            except: pass
        if self.playwright:
            try: self.playwright.stop()
            except: pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def _ensure_valid_session(self):
        """Ensure we have a valid browser session"""
        try:
            if not self.page or self.page.is_closed():
                print("Session invalid, reinitializing browser...")
                self._init_browser()
                return True
            return False
        except:
            self._init_browser()
            return True
        
    def handle_cookie_consent(self):
        """Handle cookie consent popup"""
        try:
            # Wait a bit for consent UI to render
            self.page.wait_for_timeout(1500)

            # Common selectors for consent/deny buttons
            selectors = [
                "#uc-deny-all-button",
                "#uc-accept-all-button",
                "button[data-testid='uc-accept-all-button']",
                "button[data-testid='uc-deny-all-button']",
                "button:has-text('Deny')",
                "button:has-text('Reject')",
                "button:has-text('Accept All')",
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll"
            ]

            # Try to click in main frame
            for selector in selectors:
                if self.page.is_visible(selector):
                    try:
                        self.page.click(selector)
                        print("Cookie consent dismissed")
                        self.page.wait_for_timeout(500)
                        return
                    except: pass

            # Try to click in iframes (common for CMPs)
            for frame in self.page.frames:
                for selector in selectors:
                    try:
                        if frame.is_visible(selector):
                            frame.click(selector)
                            print("Cookie consent dismissed (in frame)")
                            self.page.wait_for_timeout(500)
                            return
                    except: pass
            
            # Remove overlays via JS fallback
            self.page.evaluate("""
                const selectors = [
                    '#uc-main-dialog', '#main-view', '#uc-banner', '#uc-cmp-container',
                    '#uc-overlay', '.uc-overlay', '#CybotCookiebotDialog',
                    '#CybotCookiebotDialogBodyUnderlay', '#usercentrics-root'
                ];
                selectors.forEach(sel => {
                    const els = document.querySelectorAll(sel);
                    els.forEach(el => el.remove());
                });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            """)

        except Exception as e:
            print(f"Error handling cookie consent: {e}")
    
    def scroll_to_bottom(self):
        """Scroll to bottom of page to load all lazy-loaded content"""
        print("  Scrolling to load all content...")
        
        try:
            last_height = self.page.evaluate("document.body.scrollHeight")
            scroll_attempts = 0
            max_attempts = 50
            
            while scroll_attempts < max_attempts:
                # Scroll down
                self.page.evaluate("window.scrollBy(0, window.innerHeight)")
                self.page.wait_for_timeout(500)
                
                new_height = self.page.evaluate("document.body.scrollHeight")
                current_scroll = self.page.evaluate("window.pageYOffset + window.innerHeight")
                
                # Check how many items are loaded
                current_items = self.page.locator("div.each_result.content_set, div.grid--listing.row.cd-listing .item").count()
                
                if scroll_attempts % 5 == 0:
                    print(f"    Scroll {scroll_attempts + 1}: {current_items} items loaded")
                
                if current_scroll >= new_height:
                    self.page.wait_for_timeout(1500) # Wait for potential new content
                    new_height_after_wait = self.page.evaluate("document.body.scrollHeight")
                    if new_height_after_wait <= new_height:
                        break # Reached bottom
                    new_height = new_height_after_wait

                last_height = new_height
                scroll_attempts += 1
            
            # Scroll back to top
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(1000)
            
        except Exception as e:
            print(f"Error scrolling: {e}")
    
    def sanitize_filename(self, filename):
        """Remove invalid characters from filename"""
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = ' '.join(filename.split())
        if len(filename) > 200:
            filename = filename[:200]
        return filename
    
    def get_category_links(self):
        """Get all category links from the main page"""
        print("Fetching category links...")
        self._ensure_valid_session()
        
        try:
            self.page.goto(self.start_url, wait_until="domcontentloaded")
            self.handle_cookie_consent()
            self.page.wait_for_timeout(2000)
            
            category_links = []
            # Find links
            links = self.page.query_selector_all("div.content a[href*='judgments-orders']")
            
            for link in links:
                href = link.get_attribute('href')
                text = link.inner_text().strip().replace('\xa0', ' ')
                
                if href and text:
                    # Construct full URL if relative
                    if not href.startswith('http'):
                        href = self.base_url + href if href.startswith('/') else f"{self.base_url}/{href}"
                        
                    category_links.append({
                        'url': href,
                        'name': text
                    })
                    print(f"Found category: {text}")
            
            return category_links
        except Exception as e:
            print(f"Error getting category links: {e}")
            return []
    
    def get_total_pages(self):
        """Get total number of pages from pagination"""
        try:
            # Check for pagination links
            pagination_links = self.page.query_selector_all("div.ccm-pagination-wrapper a[href*='ccm_paging_p=']")
            
            max_page = 1
            for link in pagination_links:
                href = link.get_attribute('href')
                if href and 'ccm_paging_p=' in href:
                    try:
                        page_match = re.search(r'ccm_paging_p=(\d+)', href)
                        if page_match:
                            page_num = int(page_match.group(1))
                            max_page = max(max_page, page_num)
                    except:
                        pass
            
            return max_page
        except Exception as e:
            print(f"Error getting total pages: {e}")
            return 1
    
    def scrape_listing_page(self, url, page_num=1):
        """Scrape all entries from a listing page"""
        self._ensure_valid_session()
        
        print(f"Scraping page {page_num}: {url}")
        
        target_url = url
        if page_num > 1:
            target_url = f"{url}?ccm_paging_p={page_num}&ccm_order_by=ak_date&ccm_order_by_direction=desc"
        
        try:
            self.page.goto(target_url, wait_until="domcontentloaded")
            self.handle_cookie_consent()
            
            # Wait for content
            try:
                self.page.wait_for_selector("div.col-sm-9.content-block", timeout=10000)
            except:
                print("  Timeout waiting for content block, proceeding...")
            
            self.scroll_to_bottom()
            
            entries = []
            
            # Try standard list structure first
            items = self.page.query_selector_all("div.each_result.content_set")
            is_grid_layout = False
            
            # If standard list empty, try grid layout (e.g. Joint Judicial Committee)
            if not items:
                items = self.page.query_selector_all("div.grid--listing.row.cd-listing div.col-sm-6 div.item")
                if items:
                    is_grid_layout = True
            
            print(f"  Found {len(items)} items on page (Layout: {'Grid' if is_grid_layout else 'Standard'})")
            
            for item in items:
                try:
                    title = ""
                    detail_url = ""
                    label_text = ""
                    date_text = ""
                    
                    if is_grid_layout:
                        # Grid extraction
                        h4 = item.query_selector('h4')
                        if not h4: continue
                        title = h4.inner_text().strip()
                        
                        link_tag = item.query_selector('a.download-btn') or item.query_selector('a')
                        if not link_tag: continue
                        
                        detail_url = link_tag.get_attribute('href')
                        
                        # Extract date from title (e.g. "Cassation No 1 of 2016")
                        year_match = re.search(r'\b(20\d{2})\b', title)
                        if year_match:
                            date_text = year_match.group(1)
                            
                        # Infer label/folder from title
                        if "Cassation" in title or "Judgment" in title:
                            label_text = "Judgment"
                        else:
                            label_text = "Order"
                            
                    else:
                        # Standard extraction
                        # Check for 'loaded' class (lazy load)
                        class_attr = item.get_attribute('class') or ""
                        if 'loaded' not in class_attr:
                            continue
                        
                        # Extract info
                        h4 = item.query_selector('h4')
                        if not h4: continue
                        
                        link_tag = h4.query_selector('a')
                        if not link_tag: continue
                        
                        title = link_tag.inner_text().strip()
                        detail_url = link_tag.get_attribute('href')
                        
                        label_elem = item.query_selector('p.label_small')
                        if label_elem:
                            label_text = label_elem.inner_text().strip()
                            # Extract date "January 07, 2026"
                            date_match = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', label_text)
                            if date_match:
                                date_text = date_match.group(1)
                    
                    # Ensure absolute URL
                    if detail_url and not detail_url.startswith('http'):
                        detail_url = self.base_url + detail_url if detail_url.startswith('/') else f"{self.base_url}/{detail_url}"
                        
                    if title and detail_url:
                        entries.append({
                            'title': title,
                            'url': detail_url,
                            'label': label_text,
                            'date': date_text
                        })
                        display_title = f"{title[:60]}..." if len(title) > 60 else title
                        print(f"    Found: {display_title} [{date_text}]")
                        
                except Exception as e:
                    print(f"Error parsing entry: {e}")
                    continue
            
            return entries
            
        except Exception as e:
            print(f"Error scraping listing page: {e}")
            return []
    
    def determine_folder(self, label_text):
        """Determine if it's a judgment or order based on label"""
        label_lower = (label_text or "").lower()
        if 'judgment' in label_lower:
            return self.judgments_folder
        elif 'order' in label_lower:
            return self.orders_folder
        else:
            return self.orders_folder
    
    def upload_to_s3(self, local_path, s3_key):
        """Upload file to S3 and delete local copy"""
        try:
            # Normalize path separators for S3 to forward slashes
            s3_key = s3_key.replace('\\', '/')
            self.s3_client.upload_file(local_path, self.bucket_name, s3_key)
            print(f"  Uploaded to S3: s3://{self.bucket_name}/{s3_key}")
            
            # Delete local file
            if os.path.exists(local_path):
                os.remove(local_path)
                print(f"  Deleted local file: {local_path}")
            return True
        except Exception as e:
            print(f"  Failed to upload to S3: {e}")
            return False

    def download_direct_pdf(self, entry):
        """Download PDF directly from a URL"""
        try:
            display_title = f"{entry['title'][:60]}..." if len(entry['title']) > 60 else entry['title']
            print(f"Direct download: {display_title}")
            
            # Determine path
            folder = self.determine_folder(entry.get('label', ''))
            filename = self.sanitize_filename(entry['title'])
            filepath = os.path.join(folder, f"{filename}.pdf")
            
            # Use requests to download
            # Get cookies from playwright context to be safe
            cookies = {}
            if self.context:
                try:
                    cookies = {c['name']: c['value'] for c in self.context.cookies()}
                except: pass
                
            # Disable warnings for verify=False
            requests.packages.urllib3.disable_warnings()
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Referer": "https://www.difccourts.ae/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            }
            
            response = requests.get(entry['url'], headers=headers, cookies=cookies, verify=False, stream=True, timeout=60)
            
            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(f"  Saved locally: {filepath}")
                
                # Upload to S3
                s3_key = filepath.replace('\\', '/')
                if self.upload_to_s3(filepath, s3_key):
                    self.tracker.mark_processed(entry['url'], metadata={'s3_key': s3_key, 'title': entry['title'], 'date': entry.get('date'), 'uploaded': True})
                return True
            
            # Fallback for 403 or other errors
            print(f"  Requests status {response.status_code}. Retrying with Playwright download...")
            try:
                with self.page.expect_download(timeout=60000) as download_info:
                    try:
                        # Some downloads trigger immediately on goto
                        self.page.goto(entry['url'], wait_until="domcontentloaded")
                    except Exception as e:
                        # Navigation error is sometimes expected if download starts immediately aboring nav
                        pass

                download = download_info.value
                download.save_as(filepath)
                print(f"  Saved locally (Playwright): {filepath}")
                
                # Upload to S3
                s3_key = filepath.replace('\\', '/')
                if self.upload_to_s3(filepath, s3_key):
                    self.tracker.mark_processed(entry['url'], metadata={'s3_key': s3_key, 'title': entry['title'], 'date': entry.get('date'), 'uploaded': True})
                return True
            except Exception as e:
                print(f"  Playwright download fallback failed: {e}")
                return False
                
        except Exception as e:
            print(f"  Error downloading PDF: {e}")
            return False

    def scrape_detail_page(self, entry):
        """Scrape the detail page and save as PDF"""
        # Handle direct PDF links
        if entry['url'].lower().split('?')[0].endswith('.pdf'):
            return self.download_direct_pdf(entry)

        try:
            self._ensure_valid_session()
            
            display_title = f"{entry['title'][:60]}..." if len(entry['title']) > 60 else entry['title']
            print(f"Scraping detail: {display_title}")
            
            self.page.goto(entry['url'], wait_until="domcontentloaded")
            self.handle_cookie_consent()
            
            # Wait for content
            try:
                self.page.wait_for_selector("div.each_media_listing", timeout=10000)
            except:
                print("  Content not found via selector")
                pass

            # Scroll to trigger any lazy loading
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(1000)
            self.page.evaluate("window.scrollTo(0, 0)")
            
            # Check content validity
            content_desc = self.page.query_selector("div.content_desc")
            if not content_desc or not content_desc.inner_text().strip():
                print("  Content appears empty, waiting extra time...")
                self.page.wait_for_timeout(3000)
                content_desc = self.page.query_selector("div.content_desc")
                if not content_desc:
                    print("  Content not found, skipping")
                    return False

            # Get filename and folder
            page_title = entry['title']
            title_elem = self.page.query_selector("div.each_media_listing h4")
            if title_elem:
                page_title = title_elem.inner_text().strip()
                
            folder = self.determine_folder(entry.get('label', ''))
            filename = self.sanitize_filename(page_title)
            filepath = os.path.join(folder, f"{filename}.pdf")
            html_filepath = filepath.replace('.pdf', '.html')
            
            if os.path.exists(filepath):
                print(f"  PDF already exists, skipping...")
                self.tracker.mark_processed(entry['url'], metadata={'file': filepath, 'title': page_title, 'date': entry.get('date')})
                return True
            if os.path.exists(html_filepath):
                print(f"  HTML already exists, skipping...")
                self.tracker.mark_processed(entry['url'], metadata={'file': html_filepath, 'title': page_title, 'date': entry.get('date')})
                return True

            # Prepare page for PDF printing (clean up UI)
            self.page.evaluate("""
                // Hide header, footer, navigation, and cookie overlays
                const elementsToHide = document.querySelectorAll('header, footer, nav, .header, .footer, .navigation, #uc-overlay, .overlay, .cookie-consent, .breadcrumbs_div, .search_section, .pagination, .sidebar, aside, .social-share, .share-buttons');
                elementsToHide.forEach(el => el.style.display = 'none');
                
                // Hide search forms
                const searchForms = document.querySelectorAll('form#basic_search, .search_section');
                searchForms.forEach(el => el.style.display = 'none');
                
                // Style main content
                const mainContent = document.querySelector('.each_media_listing');
                if (mainContent) {
                    mainContent.style.padding = '30px';
                    mainContent.style.margin = '0 auto';
                    mainContent.style.maxWidth = '800px';
                    mainContent.style.backgroundColor = 'white';
                }
                
                // Ensure content is visible
                const contentDesc = document.querySelector('.content_desc');
                if (contentDesc) {
                    contentDesc.style.display = 'block';
                    contentDesc.style.visibility = 'visible';
                }
                
                // Hide sidebars etc
                const otherHide = document.querySelectorAll('.col-sm-3, .sidebar, .side-menu, .related-content');
                otherHide.forEach(el => el.style.display = 'none');
            """)
            
            self.page.wait_for_timeout(1000)
            
            # Save PDF
            try:
                self.page.pdf(
                    path=filepath,
                    format="A4",
                    margin={
                        "top": "0.4in",
                        "bottom": "0.4in",
                        "left": "0.4in",
                        "right": "0.4in"
                    },
                    print_background=True
                )
                print(f"  Saved locally: {filepath}")
                
                # Upload to S3
                s3_key = filepath.replace('\\', '/')
                if self.upload_to_s3(filepath, s3_key):
                    self.tracker.mark_processed(entry['url'], metadata={'s3_key': s3_key, 'title': page_title, 'date': entry.get('date'), 'uploaded': True})
                else:
                    print("  S3 upload failed, keeping local file")
                    # Still mark processed so we don't rescrape, but maybe with a flag? 
                    # Actually if upload failed we probably want to retry later. 
                    # But the prompt says "make robust".
                    # Let's mark it as processed locally for now to avoid loops, or just NOT mark it.
                    # If I don't mark it, it will retry next time.
                    pass 

                return True
            except Exception as e:
                print(f"  PDF generation/upload failed: {e}")
                print("  Skipping HTML backup (User requested no HTML).")
                return False

        except Exception as e:
            print(f"  Error scraping detail: {e}")
            return False

    def scrape_category(self, category):
        """Scrape all pages in a category"""
        print(f"\n{'='*60}\nScraping category: {category['name']}\n{'='*60}")
        
        category_name = category['name']
        is_incremental = self.tracker.is_category_complete(category_name)
        
        self.tracker.set_category_status(category_name, 'in_progress')
        
        # Determine total pages
        try:
            self.page.goto(category['url'], wait_until="domcontentloaded")
            self.handle_cookie_consent()
            total_pages = self.get_total_pages()
            print(f"Total pages: {total_pages}")
        except:
            total_pages = 1
            print("Could not determine pages, assuming 1")

        total_downloaded = 0
        total_skipped = 0
        total_failed = 0
        items_since_restart = 0
        
        for page in range(1, total_pages + 1):
            print(f"\n--- Processing Page {page}/{total_pages} ---")
            
            entries = self.scrape_listing_page(category['url'], page)
            skipped_on_this_page = 0
            
            for i, entry in enumerate(entries, 1):
                print(f"\n  [{page}:{i}/{len(entries)}]")
                
                # Check tracker first
                if self.tracker.is_processed(entry['url']):
                    print(f"  Already processed (tracker), skipping: {entry['title'][:40]}...")
                    total_skipped += 1
                    skipped_on_this_page += 1
                    continue
                
                # Pre-check existence
                filename = self.sanitize_filename(entry['title'])
                folder = self.determine_folder(entry.get('label', ''))
                filepath = os.path.join(folder, f"{filename}.pdf")
                
                if os.path.exists(filepath):
                    print(f"  Found local file, uploading to S3: {filename[:40]}...")
                    s3_key = filepath.replace('\\', '/')
                    if self.upload_to_s3(filepath, s3_key):
                         self.tracker.mark_processed(entry['url'], metadata={'s3_key': s3_key, 'title': entry['title'], 'date': entry.get('date'), 'uploaded': True})
                    else:
                         self.tracker.mark_processed(entry['url'], metadata={'file': filepath, 'title': entry['title'], 'date': entry.get('date'), 'uploaded': False})
                    
                    total_skipped += 1
                    skipped_on_this_page += 1
                    continue
                
                # Scrape
                try:
                    success = self.scrape_detail_page(entry)
                    if success:
                        total_downloaded += 1
                    else:
                        total_failed += 1
                except Exception as e:
                    print(f"  Detailed scrape failed: {e}")
                    # Retry once with new browser
                    self._init_browser()
                    try:
                        if self.scrape_detail_page(entry):
                            total_downloaded += 1
                        else:
                            total_failed += 1
                    except:
                        total_failed += 1
                        
                # Memory management
                items_since_restart += 1
                if items_since_restart >= 40:
                    print("  Restarting browser to manage memory...")
                    self._init_browser()
                    items_since_restart = 0
                
                self.page.wait_for_timeout(1000)

            # Optimisation: If all items on this page were skipped AND we are in incremental mode (previous run was full success), stop.
            if len(entries) > 0 and skipped_on_this_page == len(entries):
                if is_incremental:
                     print(f"  [Incremental Update] All items on page {page} are already processed. Stopping category '{category_name}'.")
                     break
                else:
                     print(f"  [Resume Mode] all items on page {page} processed, but continuing scan (filling gaps due to crash)...")

        print(f"\nCategory '{category['name']}' complete. DL:{total_downloaded}, Skip:{total_skipped}, Fail:{total_failed}")
        self.tracker.set_category_status(category_name, 'completed')

    def run(self):
        """Main execution"""
        try:
            print("Starting DIFC Courts Scraper (Playwright Edition)...")
            
            categories = self.get_category_links()
            if not categories:
                print("No categories found.")
                return
            
            print(f"\nFound {len(categories)} categories to scrape")
            
            for i, category in enumerate(categories, 1):
                print(f"\n# Category {i}/{len(categories)}")
                self.scrape_category(category)
                
            print("\nAll scraping completed.")
            
        except Exception as e:
            print(f"Critical error: {e}")
        finally:
            self._close_browser()

if __name__ == "__main__":
    scraper = DIFCCourtsScraper()
    scraper.run()
