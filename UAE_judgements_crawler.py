"""
UAE DIFC Courts Judgements & Orders - Daily Crawler
====================================================
Designed to run as a daily cron job. Checks each category for NEW judgements/orders
that are not in scraper_state.json, downloads them, uploads to S3, and sends an
email summary on success.

Usage:
    python UAE_judgements_crawler.py            # Normal run
    python UAE_judgements_crawler.py --dry-run  # Preview new items without downloading

Cron (Linux):
    0 2 * * * cd /path/to/project && /path/to/python UAE_judgements_crawler.py >> /path/to/logs/cron.log 2>&1

Task Scheduler (Windows):
    Action: python  Arguments: UAE_judgements_crawler.py  Start in: C:\\...\\UAE scrapper
"""

import os
import re
import sys
import json
import time
import logging
import smtplib
import argparse
import tempfile
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

from jinja2 import Template
import boto3
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

STATE_FILE = BASE_DIR / "scraper_state.json"
CRAWLER_STATE_FILE = BASE_DIR / "crawler_state.json"

S3_BUCKET = "uae-judgements"
DIFC_BASE_URL = "https://www.difccourts.ae"
DIFC_START_URL = f"{DIFC_BASE_URL}/rules-decisions/judgments-orders"

# Email settings
NOTIFY_EMAIL = "priyanshudayal1504@gmail.com"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip().strip('"')
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip().strip('"')

def get_users_from_api():
    """
    Fetch all users from the API endpoint.
    Returns:
        List[dict]: List of user dictionaries containing 'name' and 'email'.
    """
    url = "https://www.lexiai.legal/api/law_firm/get-all-users/"
    # For local testing, one might use: url = "http://127.0.0.1:8000/api/law_firm/get-all-users/"
    
    logger.info("Fetching users from API: %s", url)
    
    try:
        headers = {
            'Content-Type': 'application/json',
            'Cache-Control': 'no-cache',
            'User-Agent': 'PostmanRuntime/7.45.0',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br'
        }
        
        response = requests.get(url, headers=headers, timeout=30, verify=True)
        
        if response.status_code == 200:
            try:
                data = response.json()
                users = data.get('users', [])
                logger.info("Successfully fetched %d users from API", len(users))
                return users
            except ValueError as e:
                logger.error("JSON decode error from API: %s", e)
                return []
        else:
            logger.error("API request failed with status %d: %s", response.status_code, response.text)
            return []
            
    except Exception as e:
        logger.error("Failed to fetch users from API: %s", e)
        return []

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_filename = LOGS_DIR / f"crawler_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logger = logging.getLogger("UAE_Crawler")
logger.setLevel(logging.DEBUG)

# File handler – detailed
fh = logging.FileHandler(log_filename, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# Console handler – concise
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger.addHandler(fh)
logger.addHandler(ch)


# ---------------------------------------------------------------------------
# State Manager  (uses existing scraper_state.json as source of truth)
# ---------------------------------------------------------------------------
class StateManager:
    """Thread-safe state manager that reads from scraper_state.json
    and maintains its own crawler_state.json for crawler-specific metadata."""

    def __init__(self):
        self.processed_urls: set = set()
        self.categories: dict = {}
        self._load()

    def _load(self):
        """Load already-processed URLs from the main scraper state file."""
        for path in [STATE_FILE, CRAWLER_STATE_FILE]:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.processed_urls.update(data.get("processed_urls", {}).keys())
                    self.categories.update(data.get("categories", {}))
                    logger.info("Loaded %d processed URLs from %s", len(data.get("processed_urls", {})), path.name)
                except Exception as e:
                    logger.warning("Could not load %s: %s", path.name, e)

    def is_processed(self, url: str) -> bool:
        return url.strip() in self.processed_urls

    def mark_processed(self, url: str, metadata: dict | None = None):
        """Mark URL processed in both runtime set and crawler state file."""
        url = url.strip()
        self.processed_urls.add(url)
        self._append_to_crawler_state(url, metadata)

    def _append_to_crawler_state(self, url: str, metadata: dict | None = None):
        """Persist to crawler_state.json (incremental, safe)."""
        try:
            data = {}
            if CRAWLER_STATE_FILE.exists():
                with open(CRAWLER_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

            if "processed_urls" not in data:
                data["processed_urls"] = {}
            if "categories" not in data:
                data["categories"] = {}

            data["processed_urls"][url] = {
                "timestamp": datetime.now().isoformat(),
                "status": "success",
                "metadata": metadata or {},
            }
            data["last_updated"] = datetime.now().isoformat()
            data["processed_count"] = len(data["processed_urls"])

            tmp = str(CRAWLER_STATE_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(CRAWLER_STATE_FILE))
        except Exception as e:
            logger.error("Failed to persist crawler state: %s", e)

    def set_category_status(self, name: str, status: str):
        self.categories[name] = {
            "status": status,
            "last_updated": datetime.now().isoformat(),
        }

    # Also sync back to the main scraper_state.json so the original scraper
    # recognizes newly-crawled URLs.
    def sync_to_main_state(self):
        """Merge crawler_state into scraper_state.json so both stay in sync."""
        try:
            if not CRAWLER_STATE_FILE.exists():
                return
            with open(CRAWLER_STATE_FILE, "r", encoding="utf-8") as f:
                crawler_data = json.load(f)

            main_data = {}
            if STATE_FILE.exists():
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    main_data = json.load(f)

            if "processed_urls" not in main_data:
                main_data["processed_urls"] = {}
            if "categories" not in main_data:
                main_data["categories"] = {}

            # Merge
            for url, meta in crawler_data.get("processed_urls", {}).items():
                if url not in main_data["processed_urls"]:
                    main_data["processed_urls"][url] = meta

            for cat, info in crawler_data.get("categories", {}).items():
                main_data["categories"][cat] = info

            main_data["last_updated"] = datetime.now().isoformat()
            main_data["processed_count"] = len(main_data["processed_urls"])

            tmp = str(STATE_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(main_data, f, indent=2)
            os.replace(tmp, str(STATE_FILE))
            logger.info("Synced crawler state -> scraper_state.json (%d total URLs)", main_data["processed_count"])
        except Exception as e:
            logger.error("Failed to sync states: %s", e)


# ---------------------------------------------------------------------------
# Email Notifier
# ---------------------------------------------------------------------------
def send_email_notification(new_items: list[dict], run_stats: dict, user_data: dict = None, debug: bool = False, to_email: str = None):
    """
    Send an HTML email summarising newly scraped judgements/orders using Jinja2 template.
    
    Args:
        new_items: List of judgment dicts
        run_stats: statistics of the run
        user_data: Dictionary with 'name' and 'email' of the recipient
        debug: If true, enables SMTP debug output
        to_email: Override recipient email (useful for testing single send)
    """
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        logger.warning("Email credentials not configured – skipping notification.")
        return False
    
    # Determine recipient
    if to_email:
        target_email = to_email
        target_name = "Valued User" 
    elif user_data:
        target_email = user_data.get('email')
        target_name = user_data.get('name', 'Valued User')
    else:
        # Fallback to admin email if no user specified
        target_email = NOTIFY_EMAIL
        target_name = "Admin"

    if not target_email:
        logger.warning("No target email provided.")
        return False

    try:
        if debug:
            logger.info("DEBUG: Sending from '%s' to '%s'", EMAIL_HOST_USER, target_email)

        subject = f"[UAE DIFC Crawler] {len(new_items)} new judgement(s)/order(s) scraped – {datetime.now().strftime('%Y-%m-%d')}"

        # Prepare data for template
        from collections import Counter
        
        all_judgments_for_template = []
        available_categories = set()
        
        for item in new_items:
            # Normalize category
            category = item.get('category', 'General')
            if not category: category = 'General'
            available_categories.add(category)
            
            all_judgments_for_template.append({
                'title': item.get('title', 'Untitled'),
                'court': "DIFC Courts",
                'category': category,
                'date': item.get('date', 'Unknown'),
                'link': item.get('url', '#'),
                'is_new': True,
                'status': 'success'
            })
            
        cat_counts = Counter(item.get('category', 'General') for item in new_items)
        law_categories_text = []
        for category, count in cat_counts.most_common(5):
             law_categories_text.append(f'<span class="highlight">{category} ({count})</span>')
        
        law_categories_summary = ' • '.join(law_categories_text) if law_categories_text else 'No categorized judgments today'


        email_template = """<!DOCTYPE html>
  <html lang="en">
  <head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
    <title>Daily Legal Insights - {{ total_judgments }} Fresh Judgments</title>
    <style>
      body {
        margin: 0;
        padding: 0;
        background: linear-gradient(135deg, #f4f8fb 0%, #e8f2f6 100%);
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        color: #333;
      }

      /* Signature Section */
      .signature-section {
        background: #ffffff;
        border-radius: 12px;
        padding: 30px 25px;
        margin: 30px 0 20px;
        border-top: 1px solid #e5e9ec;
      }

      .signature-content {
        max-width: 400px;
      }

      .signature-text {
        font-size: 16px;
        color: #333;
        margin: 0 0 15px 0;
        font-weight: 500;
      }

      .signature-details {
        display: flex;
        align-items: flex-start;
        gap: 15px;
      }

      .signature-image {
        width: 120px;
        height: auto;
        max-height: 60px;
        object-fit: contain;
      }

      .signature-info {
        flex: 1;
      }

      .signature-name {
        font-size: 18px;
        font-weight: 700;
        color: #000F24;
        margin: 0 0 5px 0;
      }

      .signature-person {
        font-size: 16px;
        color: #BF8F4C;
        font-weight: 600;
        margin: 0;
      }

      @media only screen and (max-width: 600px) {
        .signature-section {
          padding: 20px 15px;
        }
        
        .signature-details {
          flex-direction: column;
          align-items: flex-start;
          gap: 10px;
        }
        
        .signature-image {
          width: 100px;
          max-height: 50px;
        }
      }

      
      .container {
        max-width: 650px;
        margin: auto;
        background: #ffffff;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 10px 30px rgba(0,0,0,0.12);
        margin-top: 20px;
        margin-bottom: 20px;
      }
      .header {
        background: linear-gradient(135deg, #000F24 0%, #1a2332 100%);
        color: #fff;
        text-align: center;
        padding: 40px 20px;
        position: relative;
        overflow: hidden;
      }
      .header::before {
        content: '';
        position: absolute;
        top: -50%;
        left: -50%;
        width: 200%;
        height: 200%;
        background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
        animation: pulse 4s ease-in-out infinite;
      }
      @keyframes pulse {
        0%, 100% { transform: scale(1); opacity: 0.3; }
        50% { transform: scale(1.1); opacity: 0.5; }
      }
      .header h1 {
        margin: 15px 0 10px;
        font-size: 28px;
        font-weight: 700;
        position: relative;
        z-index: 2;
      }
      .header p {
        margin: 0;
        font-size: 16px;
        opacity: 0.95;
        position: relative;
        z-index: 2;
      }
      .logo-wrapper {
        display: inline-block;
        padding: 4px;
        border-radius: 50%;
        background: linear-gradient(135deg, #BF8F4C, #DEA63B, #F7BE45);
        position: relative;
        z-index: 2;
      }
      .logo-circle {
        width: 80px;
        height: 80px;
        border-radius: 50%;
        background: #fff;
        object-fit: cover;
        display: block;
        font-size: 40px;
        line-height: 80px;
        text-align: center;
      }
      .stats-banner {
        background: linear-gradient(90deg, #BF8F4C 0%, #DEA63B 50%, #F7BE45 100%);
        padding: 20px;
        text-align: center;
        color: #fff;
      }
      .stats-number {
        font-size: 48px;
        font-weight: 800;
        margin: 0;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
      }
      .stats-text {
        font-size: 18px;
        margin: 5px 0 0;
        font-weight: 500;
      }
      .content {
        padding: 30px 25px;
      }
      .intro-text {
        font-size: 18px;
        font-weight: 600;
        color: #000F24;
        margin-bottom: 15px;
        text-align: center;
      }
      .description {
        font-size: 16px;
        margin-bottom: 30px;
        color: #555;
        text-align: center;
        line-height: 1.6;
      }
      .section {
        margin-bottom: 30px;
      }
      .section-title {
        font-size: 20px;
        font-weight: 700;
        color: #000F24;
        margin-bottom: 20px;
        text-align: center;
        position: relative;
      }
      .section-title::after {
        content: '';
        position: absolute;
        bottom: -8px;
        left: 50%;
        transform: translateX(-50%);
        width: 60px;
        height: 3px;
        background: linear-gradient(90deg, #BF8F4C, #DEA63B);
        border-radius: 2px;
      }

      /* Judgments Display */
      .judgments-list {
        background: #f8fafb;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 25px;
      }
      .judgment-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px 0;
        border-bottom: 1px solid #e5e9ec;
        transition: all 0.2s ease;
      }
      .judgment-item:last-child {
        border-bottom: none;
      }
      .judgment-item:hover {
        background: rgba(191, 143, 76, 0.05);
      }
      .judgment-info {
        flex: 1;
      }
      .judgment-title {
        font-size: 15px;
        font-weight: 600;
        color: #000F24;
        margin-bottom: 4px;
        line-height: 1.3;
      }
      .judgment-meta {
        font-size: 12px;
        color: #777;
      }
      .judgment-actions {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      /* sent to last */
      .judgment-link {
        margin-left: auto;
        color: #fff;
        text-decoration: none;
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 500;
        transition: all 0.3s ease;
      }
      .judgment-link:hover {
        color: #649b9a;
      }

      .cta-section {
        background: linear-gradient(135deg, #f8fafb 0%, #e8f2f6 100%);
        border-radius: 12px;
        padding: 25px;
        text-align: center;
        margin-bottom: 20px;
      }
      .cta-button {
        display: inline-block;
        background: linear-gradient(135deg, #BF8F4C 0%, #DEA63B 100%);
        color: #fff;
        text-decoration: none;
        padding: 15px 30px;
        border-radius: 30px;
        font-size: 16px;
        font-weight: 600;
        margin-top: 15px;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(191, 143, 76, 0.3);
      }
      .cta-button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(191, 143, 76, 0.4);
      }
      .footer {
        background: #f7f9fa;
        padding: 20px;
        text-align: center;
        font-size: 13px;
        color: #888;
        border-top: 1px solid #e5e9ec;
      }
      .highlight {
        color: #BF8F4C;
        font-weight: 600;
      }
      .badge {
        display: inline-block;
        background: #000F24;
        color: #fff;
        border-radius: 20px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 500;
        margin-left: 8px;
      }
      .no-judgments {
        text-align: center;
        padding: 40px 20px;
        color: #666;
      }
      .no-judgments h3 {
        color: #000F24;
        margin-bottom: 15px;
      }

      @media only screen and (max-width: 600px) {
        .container {
          margin: 10px;
          border-radius: 12px;
        }
        .header h1 { font-size: 24px; }
        .stats-number { font-size: 36px; }
        .content { padding: 25px 20px; }
        .cta-section { padding: 20px 15px; }
        .judgment-item {
          flex-direction: column;
          align-items: flex-start;
          gap: 10px;
        }
        .judgment-actions {
          width: 100%;
          justify-content: flex-end;
        }
      }
    </style>
  </head>
  <body>
    <div class="container">
      <!-- Header -->
      <div class="header">
        <div class="logo-wrapper">
          <div class="logo-circle">⚖️</div>
        </div>
        <h1>🏛️ Daily Legal Intelligence</h1>
        <p>Fresh judgments delivered to your inbox • {{ target_date }}</p>
      </div>
      
      <!-- Stats Banner -->
      <div class="stats-banner">
        <div class="stats-number">{{ total_judgments }}+</div>
        <div class="stats-text">New Judgments Available Today</div>
      </div>
      
      <!-- Content -->
      <div class="content">
        <p class="intro-text">Hello {{ user_name }}, your legal edge awaits! ⚡</p>
        
        {% if total_judgments > 0 %}
        <p class="description">
          We've processed and analyzed <span class="highlight">{{ total_judgments }} fresh judgments</span> from courts across UAE today. 
          <span class="highlight">{{ successful_judgments }} judgments</span> are now ready for your legal research and analysis.
        </p>

        <!-- All Judgments Section -->
        <div class="section">
          <div class="section-title">📋 All Today's Judgments ({{ total_judgments }})</div>
          <div class="judgments-list">
            {% for judgment in all_judgments %}
            <div class="judgment-item">
              <div class="judgment-info">
                <div class="judgment-title">{{ judgment.title }}</div>
                <div class="judgment-meta">{{ judgment.court }} • {{ judgment.category }} • {{ judgment.date }}</div>
              </div>
            </div>
            {% endfor %}
          </div>
        </div>
        
        {% else %}
        <!-- No Judgments Section -->
        <div class="no-judgments">
          <h3>📋 No New Judgments Today</h3>
          <p>No new judgments were processed for {{ target_date }}. This could indicate:</p>
          <ul style="text-align: left; display: inline-block; margin: 15px 0;">
            <li>Courts did not publish new judgments on this date</li>
            <li>All available judgments were previously processed</li>
            <li>Temporary technical issues with court databases</li>
          </ul>
          <p><strong>Stay tuned!</strong> We'll continue monitoring and notify you as soon as new judgments become available.</p>
        </div>
        {% endif %}

        <!-- CTA Section -->
        <div class="cta-section">
          <h3 style="color: #000F24; margin-top: 0;">🚀 Ready to Dive Deeper?</h3>
          <p>Access your LeXi AI dashboard for AI-powered legal research, case analysis, and intelligent search across all judgments.</p>
          <a href="https://www.lexiai.legal" class="cta-button">Open LeXi AI </a>
        </div>
        
        <p class="description">
          💡 <strong>Pro Tip:</strong> Use our AI-powered LeXi AI Legal Assistant to streamline your research process. Get instant access to relevant case law, statutes, and legal insights tailored to your needs. Create drafts within seconds.
        </p>
      </div>
      
      <div class="signature-section">
        <div class="signature-content">
          <p class="signature-text">Best regards,</p>
          <div class="signature-details">
            <!-- <img src="sign.png" alt="Signature" class="signature-image"> -->
            <div class="signature-info">
              <p class="signature-name">Team LeXi AI</p>
              <p class="signature-person">Onkar Rana</p>
              <p class="signature-person">Founder & CEO</p>
            </div>
          </div>
        </div>
      </div>      
      
      <!-- Footer -->
      <div class="footer">
        <p>© {{ current_year }} LeXi AI. Empowering legal professionals with AI-driven insights.</p>
        <p>This automated report contains judgments sourced from official government eCourts platforms.</p>
        <p style="margin-top: 10px; font-size: 12px; color: #aaa;">
          Sent to {{ user_email }} • For support, contact our team
        </p>
      </div>
    </div>
  </body>
  </html>"""

        template = Template(email_template)
        
        html = template.render(
            user_name=target_name,
            target_date=datetime.now().strftime('%Y-%m-%d'),
            total_judgments=len(new_items),
            successful_judgments=len(new_items),
            featured_judgments=all_judgments_for_template[:5],
            all_judgments=all_judgments_for_template,
            available_courts=["DIFC Courts"],
            available_categories=sorted(list(available_categories)),
            law_categories_summary=law_categories_summary,
            court_distribution=[("DIFC Courts", len(new_items))],
            current_year=datetime.now().year,
            user_email=target_email
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_HOST_USER
        msg["To"] = target_email
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            if debug:
                server.set_debuglevel(1)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, target_email, msg.as_string())

        logger.info("Email notification sent to %s", target_email)
        return True
    except Exception as e:
        logger.error("Failed to send email: %s\n%s", e, traceback.format_exc())

def broadcast_notifications(new_items: list[dict], run_stats: dict):
    """Fetch users from API and send notification to each."""
    users = get_users_from_api()
    if not users:
        logger.warning("No users returned from API. Skipping broadcast.")
        return

    logger.info("Starting broadcast to %d users...", len(users))
    count = 0
    for user in users:
        if send_email_notification(new_items, run_stats, user_data=user):
            count += 1
        # Be nice to the SMTP server
        time.sleep(1) 
    
    logger.info("Broadcast complete. Sent %d emails.", count)


# ---------------------------------------------------------------------------
# S3 Uploader
# ---------------------------------------------------------------------------
class S3Uploader:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
        self.bucket = S3_BUCKET

    def upload(self, local_path: str, s3_key: str) -> bool:
        try:
            s3_key = s3_key.replace("\\", "/")
            self.client.upload_file(local_path, self.bucket, s3_key)
            logger.info("Uploaded to S3: s3://%s/%s", self.bucket, s3_key)
            # Delete local file after successful upload
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.debug("Deleted local file: %s", local_path)
            return True
        except Exception as e:
            logger.error("S3 upload failed for %s: %s", s3_key, e)
            return False


# ---------------------------------------------------------------------------
# Crawler Core
# ---------------------------------------------------------------------------
class DIFCDailyCrawler:
    """Daily incremental crawler for DIFC Courts judgements & orders."""

    def __init__(self, dry_run: bool = False, notification_email: str = None):
        self.dry_run = dry_run
        self.notification_email = notification_email
        self.state = StateManager()
        self.s3 = S3Uploader()

        # Directories for temporary local storage
        self.judgments_folder = str(BASE_DIR / "judgments")
        self.orders_folder = str(BASE_DIR / "orders")
        os.makedirs(self.judgments_folder, exist_ok=True)
        os.makedirs(self.orders_folder, exist_ok=True)

        # Playwright handles
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        # Run statistics
        self.new_items: list[dict] = []
        self.stats = {
            "categories_scanned": 0,
            "pages_scanned": 0,
            "new_downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }

    # ---- Browser lifecycle ----
    def _start_browser(self):
        self._stop_browser()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        self.context.set_default_timeout(30000)
        self.page = self.context.new_page()
        logger.info("Browser session started.")

    def _stop_browser(self):
        for attr in ("page", "context", "browser", "playwright"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    if attr == "playwright":
                        obj.stop()
                    else:
                        obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _ensure_browser(self):
        try:
            if not self.page or self.page.is_closed():
                self._start_browser()
        except Exception:
            self._start_browser()

    # ---- Cookie dismissal ----
    def _dismiss_cookies(self):
        try:
            self.page.wait_for_timeout(1500)
            selectors = [
                "#uc-deny-all-button", "#uc-accept-all-button",
                "button[data-testid='uc-accept-all-button']",
                "button[data-testid='uc-deny-all-button']",
                "button:has-text('Deny')", "button:has-text('Reject')",
                "button:has-text('Accept All')",
            ]
            for sel in selectors:
                if self.page.is_visible(sel):
                    try:
                        self.page.click(sel)
                        self.page.wait_for_timeout(500)
                        return
                    except Exception:
                        pass
            for frame in self.page.frames:
                for sel in selectors:
                    try:
                        if frame.is_visible(sel):
                            frame.click(sel)
                            self.page.wait_for_timeout(500)
                            return
                    except Exception:
                        pass
            # JS fallback
            self.page.evaluate("""
                const sels = ['#uc-main-dialog','#main-view','#uc-banner','#uc-cmp-container',
                    '#uc-overlay','.uc-overlay','#CybotCookiebotDialog',
                    '#CybotCookiebotDialogBodyUnderlay','#usercentrics-root'];
                sels.forEach(s => document.querySelectorAll(s).forEach(e => e.remove()));
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception as e:
            logger.debug("Cookie dismiss error (non-fatal): %s", e)

    # ---- Helpers ----
    @staticmethod
    def _sanitize_filename(name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = " ".join(name.split())
        return name[:200]

    def _determine_folder(self, label: str) -> str:
        ll = (label or "").lower()
        if "judgment" in ll:
            return self.judgments_folder
        return self.orders_folder

    def _scroll_page(self):
        """Scroll to bottom to trigger lazy-loaded content."""
        try:
            for _ in range(50):
                self.page.evaluate("window.scrollBy(0, window.innerHeight)")
                self.page.wait_for_timeout(400)
                at_bottom = self.page.evaluate(
                    "window.pageYOffset + window.innerHeight >= document.body.scrollHeight"
                )
                if at_bottom:
                    self.page.wait_for_timeout(1500)
                    still_bottom = self.page.evaluate(
                        "window.pageYOffset + window.innerHeight >= document.body.scrollHeight"
                    )
                    if still_bottom:
                        break
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(800)
        except Exception as e:
            logger.debug("Scroll error: %s", e)

    # ---- Scraping logic ----
    def get_categories(self) -> list[dict]:
        """Fetch category links from the main judgments-orders page."""
        self._ensure_browser()
        logger.info("Fetching categories from %s", DIFC_START_URL)
        self.page.goto(DIFC_START_URL, wait_until="domcontentloaded")
        self._dismiss_cookies()
        self.page.wait_for_timeout(2000)

        categories = []
        links = self.page.query_selector_all("div.content a[href*='judgments-orders']")
        for link in links:
            href = link.get_attribute("href")
            text = link.inner_text().strip().replace("\xa0", " ")
            if href and text:
                if not href.startswith("http"):
                    href = DIFC_BASE_URL + (href if href.startswith("/") else f"/{href}")
                categories.append({"url": href, "name": text})
                logger.info("  Category: %s", text)
        return categories

    def get_total_pages(self) -> int:
        try:
            page_links = self.page.query_selector_all("div.ccm-pagination-wrapper a[href*='ccm_paging_p=']")
            max_page = 1
            for lnk in page_links:
                href = lnk.get_attribute("href") or ""
                m = re.search(r"ccm_paging_p=(\d+)", href)
                if m:
                    max_page = max(max_page, int(m.group(1)))
            return max_page
        except Exception:
            return 1

    def scrape_listing_page(self, url: str, page_num: int = 1) -> list[dict]:
        """Scrape entries from a category listing page."""
        self._ensure_browser()
        target = url
        if page_num > 1:
            target = f"{url}?ccm_paging_p={page_num}&ccm_order_by=ak_date&ccm_order_by_direction=desc"

        logger.info("  Listing page %d: %s", page_num, target)
        self.page.goto(target, wait_until="domcontentloaded")
        self._dismiss_cookies()

        try:
            self.page.wait_for_selector("div.col-sm-9.content-block", timeout=10000)
        except Exception:
            pass

        self._scroll_page()

        entries = []
        # Standard layout
        items = self.page.query_selector_all("div.each_result.content_set")
        is_grid = False
        if not items:
            items = self.page.query_selector_all("div.grid--listing.row.cd-listing div.col-sm-6 div.item")
            is_grid = bool(items)

        for item in items:
            try:
                title = ""
                detail_url = ""
                label_text = ""
                date_text = ""

                if is_grid:
                    h4 = item.query_selector("h4")
                    if not h4:
                        continue
                    title = h4.inner_text().strip()
                    link_tag = item.query_selector("a.download-btn") or item.query_selector("a")
                    if not link_tag:
                        continue
                    detail_url = link_tag.get_attribute("href")
                    year_m = re.search(r"\b(20\d{2})\b", title)
                    if year_m:
                        date_text = year_m.group(1)
                    label_text = "Judgment" if any(w in title for w in ("Cassation", "Judgment")) else "Order"
                else:
                    cls = item.get_attribute("class") or ""
                    if "loaded" not in cls:
                        continue
                    h4 = item.query_selector("h4")
                    if not h4:
                        continue
                    link_tag = h4.query_selector("a")
                    if not link_tag:
                        continue
                    title = link_tag.inner_text().strip()
                    detail_url = link_tag.get_attribute("href")
                    label_elem = item.query_selector("p.label_small")
                    if label_elem:
                        label_text = label_elem.inner_text().strip()
                        date_m = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{4})", label_text)
                        if date_m:
                            date_text = date_m.group(1)

                if detail_url and not detail_url.startswith("http"):
                    detail_url = DIFC_BASE_URL + (detail_url if detail_url.startswith("/") else f"/{detail_url}")

                if title and detail_url:
                    entries.append({
                        "title": title,
                        "url": detail_url,
                        "label": label_text,
                        "date": date_text,
                    })
            except Exception as e:
                logger.debug("Parse error on item: %s", e)

        logger.info("    Found %d items (layout=%s)", len(entries), "grid" if is_grid else "standard")
        return entries

    # ---- Download & upload ----
    def _download_direct_pdf(self, entry: dict, category_name: str) -> bool:
        """Download a direct PDF link via requests, fallback to Playwright."""
        folder = self._determine_folder(entry.get("label", ""))
        filename = self._sanitize_filename(entry["title"])
        filepath = os.path.join(folder, f"{filename}.pdf")
        s3_key = filepath.replace("\\", "/")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.difccourts.ae/",
        }
        cookies = {}
        try:
            if self.context:
                cookies = {c["name"]: c["value"] for c in self.context.cookies()}
        except Exception:
            pass

        requests.packages.urllib3.disable_warnings()
        try:
            resp = requests.get(entry["url"], headers=headers, cookies=cookies,
                                verify=False, stream=True, timeout=60)
            if resp.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
            else:
                raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:
            logger.warning("Requests download failed (%s), trying Playwright...", e)
            try:
                with self.page.expect_download(timeout=60000) as dl_info:
                    self.page.goto(entry["url"], wait_until="domcontentloaded")
                dl_info.value.save_as(filepath)
            except Exception as e2:
                logger.error("Playwright download also failed: %s", e2)
                return False

        # Upload to S3
        if self.s3.upload(filepath, s3_key):
            meta = {"s3_key": s3_key, "title": entry["title"], "date": entry.get("date"), "uploaded": True}
            self.state.mark_processed(entry["url"], meta)
            self._record_new_item(entry, category_name, s3_key)
            return True
        return False

    def _download_page_pdf(self, entry: dict, category_name: str) -> bool:
        """Render detail page to PDF, upload to S3."""
        self._ensure_browser()
        self.page.goto(entry["url"], wait_until="domcontentloaded")
        self._dismiss_cookies()

        try:
            self.page.wait_for_selector("div.each_media_listing", timeout=10000)
        except Exception:
            pass

        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.wait_for_timeout(1000)
        self.page.evaluate("window.scrollTo(0, 0)")

        # Check content
        content = self.page.query_selector("div.content_desc")
        if not content or not content.inner_text().strip():
            self.page.wait_for_timeout(3000)
            content = self.page.query_selector("div.content_desc")
            if not content:
                logger.warning("Empty content for %s", entry["url"])
                return False

        # Get title from detail page
        page_title = entry["title"]
        te = self.page.query_selector("div.each_media_listing h4")
        if te:
            page_title = te.inner_text().strip()

        folder = self._determine_folder(entry.get("label", ""))
        filename = self._sanitize_filename(page_title)
        filepath = os.path.join(folder, f"{filename}.pdf")
        s3_key = filepath.replace("\\", "/")

        # Clean the page for PDF
        self.page.evaluate("""
            document.querySelectorAll(
                'header,footer,nav,.header,.footer,.navigation,#uc-overlay,.overlay,' +
                '.cookie-consent,.breadcrumbs_div,.search_section,.pagination,.sidebar,' +
                'aside,.social-share,.share-buttons,form#basic_search,.col-sm-3,.side-menu,.related-content'
            ).forEach(e => e.style.display = 'none');
            const mc = document.querySelector('.each_media_listing');
            if (mc) { mc.style.padding='30px'; mc.style.margin='0 auto'; mc.style.maxWidth='800px'; mc.style.backgroundColor='white'; }
            const cd = document.querySelector('.content_desc');
            if (cd) { cd.style.display='block'; cd.style.visibility='visible'; }
        """)
        self.page.wait_for_timeout(800)

        try:
            self.page.pdf(
                path=filepath,
                format="A4",
                margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
                print_background=True,
            )
        except Exception as e:
            logger.error("PDF generation failed for %s: %s", entry["url"], e)
            return False

        if self.s3.upload(filepath, s3_key):
            meta = {"s3_key": s3_key, "title": page_title, "date": entry.get("date"), "uploaded": True}
            self.state.mark_processed(entry["url"], meta)
            self._record_new_item(entry, category_name, s3_key)
            return True
        return False

    def download_entry(self, entry: dict, category_name: str) -> bool:
        """Download a single judgement/order entry."""
        if entry["url"].lower().split("?")[0].endswith(".pdf"):
            return self._download_direct_pdf(entry, category_name)
        return self._download_page_pdf(entry, category_name)

    def _record_new_item(self, entry: dict, category_name: str, s3_key: str):
        """Track a newly-scraped item for the email report."""
        item_type = "Judgment" if "judgment" in (entry.get("label", "") or "").lower() else "Order"
        self.new_items.append({
            "title": entry["title"],
            "url": entry["url"],
            "category": category_name,
            "type": item_type,
            "date": entry.get("date", "N/A"),
            "s3_key": s3_key,
        })

    # ---- Main crawl loop ----
    def crawl_category(self, category: dict):
        """Crawl one category – only download new (unprocessed) entries."""
        name = category["name"]
        url = category["url"]
        logger.info("=" * 60)
        logger.info("Crawling category: %s", name)
        logger.info("=" * 60)

        self.state.set_category_status(name, "in_progress")
        self.stats["categories_scanned"] += 1

        self._ensure_browser()
        self.page.goto(url, wait_until="domcontentloaded")
        self._dismiss_cookies()
        total_pages = self.get_total_pages()
        logger.info("  Total pages: %d", total_pages)

        items_since_restart = 0

        for pg in range(1, total_pages + 1):
            self.stats["pages_scanned"] += 1
            entries = self.scrape_listing_page(url, pg)

            new_on_page = 0
            skipped_on_page = 0

            for idx, entry in enumerate(entries, 1):
                if self.state.is_processed(entry["url"]):
                    self.stats["skipped"] += 1
                    skipped_on_page += 1
                    continue

                # New entry found
                new_on_page += 1
                logger.info("  [NEW] [%d/%d] %s", idx, len(entries), entry["title"][:80])

                if self.dry_run:
                    logger.info("    (dry-run – skipping download)")
                    self._record_new_item(entry, name, "DRY_RUN")
                    self.stats["new_downloaded"] += 1
                    continue

                try:
                    success = self.download_entry(entry, name)
                    if success:
                        self.stats["new_downloaded"] += 1
                    else:
                        self.stats["failed"] += 1
                except Exception as e:
                    logger.error("  Download error: %s", e)
                    # Retry once with fresh browser
                    try:
                        self._start_browser()
                        if self.download_entry(entry, name):
                            self.stats["new_downloaded"] += 1
                        else:
                            self.stats["failed"] += 1
                    except Exception:
                        self.stats["failed"] += 1
                        logger.error("  Retry also failed for %s", entry["url"])

                items_since_restart += 1
                if items_since_restart >= 40:
                    logger.info("  Restarting browser for memory management...")
                    self._start_browser()
                    items_since_restart = 0

                time.sleep(1)  # Rate limiting

            # Incremental optimization: if all items on page already processed,
            # and we're past page 1, newer items are on earlier pages so we can
            # stop scanning deeper pages (entries are sorted desc by date).
            if entries and skipped_on_page == len(entries) and new_on_page == 0:
                logger.info("  All %d items on page %d already processed – stopping category scan.", len(entries), pg)
                break

        self.state.set_category_status(name, "completed")
        logger.info("Category '%s' done.", name)


    def run(self):
        """Main entry point for the daily crawl."""
        start_time = datetime.now()
        logger.info("=" * 70)
        logger.info("UAE DIFC Courts Daily Crawler – Started at %s", start_time.isoformat())
        logger.info("Dry-run: %s", self.dry_run)
        logger.info("=" * 70)

        try:
            self._start_browser()
            categories = self.get_categories()
            if not categories:
                logger.error("No categories found – aborting.")
                return

            logger.info("Found %d categories to crawl.", len(categories))

            for i, cat in enumerate(categories, 1):
                logger.info("\n>>> Category %d/%d", i, len(categories))
                try:
                    self.crawl_category(cat)
                except Exception as e:
                    logger.error("Category '%s' failed: %s\n%s", cat["name"], e, traceback.format_exc())
                    # Try to restart browser and continue with next category
                    try:
                        self._start_browser()
                    except Exception:
                        pass

        except Exception as e:
            logger.critical("Fatal error: %s\n%s", e, traceback.format_exc())
        finally:
            self._stop_browser()

        # Sync state
        if not self.dry_run:
            self.state.sync_to_main_state()

        # Summary
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 70)
        logger.info("CRAWL COMPLETE")
        logger.info("  Duration       : %.1f seconds", elapsed)
        logger.info("  Categories     : %d", self.stats["categories_scanned"])
        logger.info("  Pages scanned  : %d", self.stats["pages_scanned"])
        logger.info("  New downloaded : %d", self.stats["new_downloaded"])
        logger.info("  Skipped        : %d", self.stats["skipped"])
        logger.info("  Failed         : %d", self.stats["failed"])
        logger.info("=" * 70)

        # Send email if there are new items
        if self.new_items:
            if self.notification_email:
                logger.info("Sending report ONLY to specific email: %s", self.notification_email)
                user_data = {"name": "Valued User", "email": self.notification_email}
                send_email_notification(self.new_items, self.stats, user_data=user_data)
            else:
                logger.info("Found %d new items. Initiating broadcast to all users...", len(self.new_items))
                broadcast_notifications(self.new_items, self.stats)
        else:
            logger.info("No new items found – no email notifications sent.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="UAE DIFC Courts Daily Crawler")
    parser.add_argument("--dry-run", action="store_true", help="List new items without downloading")
    parser.add_argument("--test-email", action="store_true", help="Send a test email to verify configuration and exit")
    parser.add_argument("--email-to", help="Override recipient email address for testing")
    parser.add_argument("--fetch-users", action="store_true", help="Fetch and list all users from API then exit")
    parser.add_argument("--test-broadcast", action="store_true", help="Send a TEST email to ALL users fetched from API")
    args = parser.parse_args()

    # 1. Fetch users check
    if args.fetch_users:
        users = get_users_from_api()
        print(f"\nFetched {len(users)} users:")
        for u in users:
            print(f" - {u.get('name', 'No Name')} <{u.get('email', 'No Email')}>")
        return

    # 2. Test Broadcast (All Users)
    if args.test_broadcast:
        logger.info("Preparing to BROADCAST test email to ALL users from API...")
        users = get_users_from_api()
        if not users:
            logger.error("No users found. Aborting broadcast test.")
            return
        
        # Confirmation
        print(f"WARNING: You are about to send a test email to {len(users)} users.")
        confirm = input("Type 'YES' to proceed: ")
        if confirm != "YES":
            print("Aborted.")
            return

        dummy_items = [{
            "category": "TEST_BROADCAST",
            "title": "System Check - All Users Broadcast",
            "url": "https://www.difccourts.ae/rules-decisions/judgments-orders/test",
            "type": "TestType",
            "date": datetime.now().strftime('%Y-%m-%d'),
            "s3_key": "test/broadcast.pdf"
        }]
        dummy_stats = {"categories_scanned": 1, "pages_scanned": 1, "skipped": 0, "failed": 0}
        
        for user in users:
            send_email_notification(dummy_items, dummy_stats, user_data=user, debug=True)
            time.sleep(1)
        return

    # 3. Single Test Email
    if args.test_email:
        target = args.email_to or NOTIFY_EMAIL
        logger.info("Attempting to send test email to %s...", target)
        
        dummy_user = {"name": "Test User", "email": target}
        
        dummy_items = [{
            "category": "TEST_CATEGORY",
            "title": "Test Judgement Title (System Check)",
            "url": "https://www.difccourts.ae/rules-decisions/judgments-orders/test",
            "type": "TestType",
            "date": datetime.now().strftime('%Y-%m-%d'),
            "s3_key": "test/folder/document.pdf"
        }]
        dummy_stats = {
            "categories_scanned": 1,
            "pages_scanned": 1,
            "skipped": 0,
            "failed": 0
        }
        
        if send_email_notification(dummy_items, dummy_stats, user_data=dummy_user, debug=True):
            logger.info("Test email sent successfully!")
        else:
            logger.error("Failed to send test email. Check logs/credentials.")
        return

    # 4. Normal Run
    if args.email_to:
        logger.info("Running in Single-Target Mode. Report will be sent ONLY to: %s", args.email_to)
    
    crawler = DIFCDailyCrawler(dry_run=args.dry_run, notification_email=args.email_to)
    crawler.run()


if __name__ == "__main__":
    main()
