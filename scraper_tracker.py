import json
import os
import threading
from datetime import datetime

class ScraperTracker:
    def __init__(self, state_file="scraper_state.json"):
        self.state_file = state_file
        self.processed_urls = {}
        self.categories = {}
        self.lock = threading.Lock()
        self.load_state()

    def load_state(self):
        """Load state from JSON file if it exists."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.processed_urls = data.get('processed_urls', {})
                    self.categories = data.get('categories', {})
                print(f"Loaded tracking state: {len(self.processed_urls)} URLs processed.")
            except Exception as e:
                print(f"Error loading state file: {e}. Starting fresh.")
                self.processed_urls = {}
                self.categories = {}
        else:
            self.processed_urls = {}
            self.categories = {}

    def save_state(self):
        """Save current state to JSON file safely."""
        with self.lock:
            try:
                temp_file = f"{self.state_file}.tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        'last_updated': datetime.now().isoformat(),
                        'processed_count': len(self.processed_urls),
                        'categories': self.categories,
                        'processed_urls': self.processed_urls
                    }, f, indent=2)
                
                # Atomic rename/replace
                if os.path.exists(self.state_file):
                    os.replace(temp_file, self.state_file)
                else:
                    os.rename(temp_file, self.state_file)
            except Exception as e:
                print(f"Error saving state: {e}")

    def set_category_status(self, category, status):
        """Track category completion status (in_progress/completed)."""
        with self.lock:
            self.categories[category] = {
                'status': status,
                'last_updated': datetime.now().isoformat()
            }
        self.save_state()

    def is_category_complete(self, category):
        """Check if category was previously completed successfully."""
        return self.categories.get(category, {}).get('status') == 'completed'

    def is_processed(self, url):
        """Check if a URL has been successfully processed."""
        if not url: return False
        url = url.strip()
        return url in self.processed_urls and self.processed_urls[url].get('status') == 'success'

    def mark_processed(self, url, status="success", metadata=None):
        """Mark a URL as processed with metadata."""
        if not url: return
        url = url.strip()
        with self.lock:
            self.processed_urls[url] = {
                'timestamp': datetime.now().isoformat(),
                'status': status,
                'metadata': metadata or {}
            }
        self.save_state()
