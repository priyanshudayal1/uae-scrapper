"""
UAE Legislation PDF Scraper
Scrapes and downloads PDFs from https://uaelegislation.gov.ae/en
"""

import os
import sys
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service


class UAELegislationScraper:
    def __init__(self):
        self.base_url = "https://uaelegislation.gov.ae"
        self.home_url = f"{self.base_url}/en"
        
        # Set up directories
        self.base_dir = Path(__file__).parent
        self.pdfs_dir = self.base_dir / "pdfs"
        self.logs_dir = self.base_dir / "logs"
        
        self.pdfs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        
        # Set up logging
        self.setup_logging()
        
        # Categories with their sector IDs
        self.categories = {
            "Education": 55,
            "Healthcare": 43,
            "Labour_Residency_Professions": 44,
            "Economy_Business": 45,
            "Industry_Technical": 56,
            "Finance_Banking": 46,
            "Tax": 57,
            "Justice_Judiciary": 47,
            "Security_Safety": 58,
            "Telecom_Technology_Space": 49,
            "Energy_Transport_Infrastructure": 50,
            "Family_Community": 51,
            "Environment_Resources": 52,
            "Culture_Media": 53,
            "Sport": 61,
            "Faith_Religion": 59,
            "Government_Affairs": 60
        }
        
        # Session for downloads
        self.session = self._create_session()
        
        # Statistics
        self.stats = {
            "categories_processed": 0,
            "legislations_found": 0,
            "pdfs_downloaded": 0,
            "pdfs_failed": 0,
            "errors": []
        }
    
    def setup_logging(self):
        """Configure logging to file and console"""
        log_file = self.logs_dir / f"scraper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("="*80)
        self.logger.info("UAE Legislation Scraper Started")
        self.logger.info("="*80)
    
    def _create_session(self):
        """Create requests session with retry strategy"""
        session = requests.Session()
        retry = Retry(
            total=3,
            read=3,
            connect=3,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 504)
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        return session
    
    def _get_driver(self):
        """Create and return a Chrome WebDriver"""
        chrome_options = Options()
        # chrome_options.add_argument('--headless')  # Uncomment for headless mode
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    
    def get_categories_from_home(self):
        """Extract all categories from home page"""
        self.logger.info("Fetching categories from home page...")
        driver = self._get_driver()
        
        try:
            driver.get(self.home_url)
            WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.category_listing div.item_"))
            )
            
            # Take screenshot of loaded page
            time.sleep(2)
            html = driver.page_source
            self.logger.info("Successfully loaded home page")
            return html
        
        except Exception as e:
            self.logger.error(f"Error fetching home page: {str(e)}")
            self.stats['errors'].append(f"Home page fetch: {str(e)}")
            return None
        finally:
            driver.quit()
    
    def extract_legislations_from_page(self, html):
        """Extract legislation entries from HTML"""
        soup = BeautifulSoup(html, 'html.parser')
        legislations = []
        
        # Find all legislation rows
        rows = soup.find_all('div', class_='body_tr')
        
        for row in rows:
            try:
                # Get legislation link and ID
                leg_link = row.find('a', href=True)
                if not leg_link:
                    continue
                
                leg_url = leg_link.get('href', '')
                leg_id = leg_url.split('/legislations/')[-1].strip('/')
                
                # Get legislation name
                leg_name = leg_link.get_text(strip=True)
                
                # Get legislation number
                spans = row.find_all('span', class_='text_center')
                leg_number = spans[0].get_text(strip=True) if spans else "Unknown"
                year = spans[1].get_text(strip=True) if len(spans) > 1 else "Unknown"
                
                legislations.append({
                    'id': leg_id,
                    'name': leg_name,
                    'number': leg_number,
                    'year': year,
                    'url_en': f"{self.base_url}/en/legislations/{leg_id}/download",
                    'url_ar': f"{self.base_url}/ar/legislations/{leg_id}/download"
                })
            
            except Exception as e:
                self.logger.warning(f"Error parsing legislation row: {str(e)}")
                continue
        
        return legislations
    
    def get_all_pages_for_category(self, sector_id, category_name):
        """Get all legislations for a category by handling pagination"""
        all_legislations = []
        driver = self._get_driver()
        page = 1
        
        try:
            while True:
                url = f"{self.base_url}/en/legislations?sector={sector_id}&page={page}"
                self.logger.info(f"Fetching {category_name} - Page {page}")
                
                driver.get(url)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.l_t_body"))
                )
                
                time.sleep(2)
                html = driver.page_source
                
                legislations = self.extract_legislations_from_page(html)
                if not legislations:
                    self.logger.info(f"{category_name} - No more pages (Page {page})")
                    break
                
                all_legislations.extend(legislations)
                self.logger.info(f"{category_name} - Found {len(legislations)} legislations on page {page}")
                
                # Check if next page exists
                soup = BeautifulSoup(html, 'html.parser')
                next_button = soup.find('a', class_='page-link next_')
                if not next_button:
                    self.logger.info(f"{category_name} - Reached last page")
                    break
                
                page += 1
        
        except Exception as e:
            self.logger.error(f"Error fetching pages for {category_name}: {str(e)}")
            self.stats['errors'].append(f"{category_name} pagination: {str(e)}")
        
        finally:
            driver.quit()
        
        return all_legislations
    
    def download_pdf(self, url, filename, category_name):
        """Download a single PDF file"""
        try:
            response = self.session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            
            # Create category directory if it doesn't exist
            category_dir = self.pdfs_dir / category_name
            category_dir.mkdir(parents=True, exist_ok=True)
            
            file_path = category_dir / filename
            
            # Check if file already exists
            if file_path.exists():
                self.logger.info(f"File already exists: {filename}")
                return True
            
            # Write file
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            self.logger.info(f"Downloaded: {filename}")
            self.stats['pdfs_downloaded'] += 1
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to download {filename}: {str(e)}")
            self.stats['pdfs_failed'] += 1
            self.stats['errors'].append(f"{filename}: {str(e)}")
            return False
    
    def process_category(self, category_name, sector_id):
        """Process a single category: get all legislations and download PDFs"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"Processing Category: {category_name} (Sector: {sector_id})")
        self.logger.info(f"{'='*80}")
        
        # Get all legislations for this category
        legislations = self.get_all_pages_for_category(sector_id, category_name)
        
        if not legislations:
            self.logger.warning(f"No legislations found for {category_name}")
            return
        
        self.logger.info(f"Total legislations found for {category_name}: {len(legislations)}")
        self.stats['legislations_found'] += len(legislations)
        
        # Download PDFs
        for idx, leg in enumerate(legislations, 1):
            self.logger.info(f"[{idx}/{len(legislations)}] Processing: {leg['name'][:60]}...")
            
            # Download English version
            filename_en = f"{leg['id']}_{leg['number']}_{leg['year']}_EN.pdf"
            self.download_pdf(leg['url_en'], filename_en, category_name)
            
            # Download Arabic version
            filename_ar = f"{leg['id']}_{leg['number']}_{leg['year']}_AR.pdf"
            self.download_pdf(leg['url_ar'], filename_ar, category_name)
            
            # Small delay between downloads to avoid overloading
            time.sleep(0.5)
        
        self.stats['categories_processed'] += 1
    
    def run(self, specific_categories=None):
        """Run the scraper"""
        try:
            categories_to_process = specific_categories or self.categories
            
            total_categories = len(categories_to_process)
            for idx, (cat_name, sector_id) in enumerate(categories_to_process.items(), 1):
                self.logger.info(f"\n[{idx}/{total_categories}] Starting {cat_name}...")
                self.process_category(cat_name, sector_id)
                time.sleep(1)  # Delay between categories
            
            self.print_summary()
        
        except KeyboardInterrupt:
            self.logger.warning("\nScraper interrupted by user")
            self.print_summary()
        except Exception as e:
            self.logger.error(f"Fatal error: {str(e)}")
            self.stats['errors'].append(f"Fatal: {str(e)}")
            self.print_summary()
    
    def print_summary(self):
        """Print summary statistics"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info("SCRAPING SUMMARY")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Categories Processed: {self.stats['categories_processed']}")
        self.logger.info(f"Legislations Found: {self.stats['legislations_found']}")
        self.logger.info(f"PDFs Downloaded: {self.stats['pdfs_downloaded']}")
        self.logger.info(f"PDFs Failed: {self.stats['pdfs_failed']}")
        
        if self.stats['errors']:
            self.logger.info(f"\nErrors ({len(self.stats['errors'])}):")
            for error in self.stats['errors'][-10:]:  # Show last 10 errors
                self.logger.info(f"  - {error}")
        
        self.logger.info(f"{'='*80}")


def main():
    """Main entry point"""
    scraper = UAELegislationScraper()
    
    # Option to scrape specific categories or all
    if len(sys.argv) > 1:
        # Example: python uae_scraper.py Education Healthcare
        specific_cats = {}
        for cat_name in sys.argv[1:]:
            if cat_name in scraper.categories:
                specific_cats[cat_name] = scraper.categories[cat_name]
        
        if specific_cats:
            scraper.logger.info(f"Processing specific categories: {list(specific_cats.keys())}")
            scraper.run(specific_cats)
        else:
            scraper.logger.error("Invalid category names provided")
    else:
        scraper.logger.info("Processing all categories")
        scraper.run()


if __name__ == "__main__":
    main()
