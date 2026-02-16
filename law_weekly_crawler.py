"""
UAE Legislation – Weekly Crawler
=================================
Designed to run as a weekly cron job. Checks for NEW legislations on
uaelegislation.gov.ae, downloads PDFs, uploads to S3, and sends an
email summary to all users.

Uses the same crawler_state.json to detect which legislations are new,
downloads only those, and stops when it hits 2 consecutive pages of
already-downloaded items.

Usage:
    python law_weekly_crawler.py                 # Normal run (headless)
    python law_weekly_crawler.py --visible       # Show browser window
    python law_weekly_crawler.py --dry-run       # Preview without downloading
    python law_weekly_crawler.py --test-email    # Send test email and exit
    python law_weekly_crawler.py --fetch-users   # List API users and exit

Cron (Linux):
    0 3 * * 1 cd /path/to/project && /path/to/python law_weekly_crawler.py >> /path/to/logs/law_cron.log 2>&1

Task Scheduler (Windows):
    Action: python  Arguments: law_weekly_crawler.py  Start in: C:\\...\\UAE scrapper
"""

import sys
import os
import time
import logging
import argparse
import smtplib
import traceback
import requests
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

from jinja2 import Template
from dotenv import load_dotenv

# Reuse the scraper engine
from law_only_uae import scrape_legislations, TARGET_URL

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

NOTIFY_EMAIL = "priyanshudayal1504@gmail.com"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip().strip('"')
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip().strip('"')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_filename = LOGS_DIR / f"law_crawler_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logger = logging.getLogger("UAE_Law_Weekly")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(log_filename, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger.addHandler(fh)
logger.addHandler(ch)


# ---------------------------------------------------------------------------
# User Fetcher (same API as UAE_judgements_crawler.py)
# ---------------------------------------------------------------------------
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
                # users = data.get('users', [])
                users = [{
                    "name": "Divyanshu",
                    "email": "divyanshukaintura789@gmail.com"
                },
                {
                    "name": "priyanshu dayal",
                    "email": "priyanshudayal1504@gmail.com"
                },
                {
                    "name": "Piyushhh Dayalll",
                    "email": "piyushdayal108@gmail.com"
                }
                ]
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
# Email Notification
# ---------------------------------------------------------------------------
def send_email_notification(new_items: list, run_stats: dict, user_data: dict = None,
                            debug: bool = False, to_email: str = None):
    """
    Send an HTML email summarising newly scraped UAE legislations.

    Args:
        new_items:  List of legislation dicts (from scrape_legislations)
        run_stats:  Statistics dict from the run
        user_data:  Dictionary with 'name' and 'email' of the recipient
        debug:      If true, enables SMTP debug output
        to_email:   Override recipient email (useful for testing single send)
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
        target_email = NOTIFY_EMAIL
        target_name = "Admin"

    if not target_email:
        logger.warning("No target email provided.")
        return False

    try:

        subject = (
            f"📜 Weekly UAE Legislation Update - {datetime.now().strftime('%Y-%m-%d')} "
            f"({len(new_items)} New Legislation{'s' if len(new_items) != 1 else ''} Available)"
        )

        # Prepare data for template
        all_legislations_for_template = []
        year_set = set()

        for item in new_items:
            year = item.get("year", "Unknown")
            year_set.add(year)
            all_legislations_for_template.append({
                "title": item.get("title", "Untitled"),
                "number": item.get("number", "—"),
                "year": year,
                "url": item.get("url", "#"),
            })

        year_counts = Counter(item.get("year", "Unknown") for item in new_items)
        year_summary_parts = []
        for yr, cnt in sorted(year_counts.items(), reverse=True)[:8]:
            year_summary_parts.append(f'<span class="highlight">{yr} ({cnt})</span>')
        year_summary = " · ".join(year_summary_parts) if year_summary_parts else "No new legislations this week"

        email_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Weekly UAE Legislation Update - {{ total_legislations }} New</title>
  <style>
    body {
      margin: 0; padding: 0;
      background: linear-gradient(135deg, #f4f8fb 0%, #e8f2f6 100%);
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      color: #333;
    }
    .container {
      max-width: 650px; margin: auto; background: #ffffff;
      border-radius: 16px; overflow: hidden;
      box-shadow: 0 10px 30px rgba(0,0,0,0.12);
      margin-top: 20px; margin-bottom: 20px;
    }
    .header {
      background: linear-gradient(135deg, #000F24 0%, #1a2332 100%);
      color: #fff; text-align: center; padding: 40px 20px;
      position: relative; overflow: hidden;
    }
    .header::before {
      content: ''; position: absolute; top: -50%; left: -50%;
      width: 200%; height: 200%;
      background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
      animation: pulse 4s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 0.3; }
      50% { transform: scale(1.1); opacity: 0.5; }
    }
    .header h1 {
      margin: 15px 0 10px; font-size: 28px; font-weight: 700;
      position: relative; z-index: 2;
    }
    .header p {
      margin: 0; font-size: 16px; opacity: 0.95;
      position: relative; z-index: 2;
    }
    .logo-wrapper {
      display: inline-block; padding: 4px; border-radius: 50%;
      background: linear-gradient(135deg, #BF8F4C, #DEA63B, #F7BE45);
      position: relative; z-index: 2;
    }
    .logo-circle {
      width: 80px; height: 80px; border-radius: 50%;
      background: #fff; object-fit: cover; display: block;
      font-size: 40px; line-height: 80px; text-align: center;
    }
    .stats-banner {
      background: linear-gradient(90deg, #BF8F4C 0%, #DEA63B 50%, #F7BE45 100%);
      padding: 20px; text-align: center; color: #fff;
    }
    .stats-number {
      font-size: 48px; font-weight: 800; margin: 0;
      text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
    }
    .stats-text { font-size: 18px; margin: 5px 0 0; font-weight: 500; }
    .content { padding: 30px 25px; }
    .intro-text {
      font-size: 18px; font-weight: 600; color: #000F24;
      margin-bottom: 15px; text-align: center;
    }
    .description {
      font-size: 16px; margin-bottom: 30px; color: #555;
      text-align: center; line-height: 1.6;
    }
    .section { margin-bottom: 30px; }
    .section-title {
      font-size: 20px; font-weight: 700; color: #000F24;
      margin-bottom: 20px; text-align: center; position: relative;
    }
    .section-title::after {
      content: ''; position: absolute; bottom: -8px; left: 50%;
      transform: translateX(-50%); width: 60px; height: 3px;
      background: linear-gradient(90deg, #BF8F4C, #DEA63B);
      border-radius: 2px;
    }
    .legislation-list {
      background: #f8fafb; border-radius: 12px;
      padding: 20px; margin-bottom: 25px;
    }
    .legislation-item {
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 0; border-bottom: 1px solid #e5e9ec;
      transition: all 0.2s ease;
    }
    .legislation-item:last-child { border-bottom: none; }
    .legislation-item:hover { background: rgba(191, 143, 76, 0.05); }
    .legislation-info { flex: 1; }
    .legislation-title {
      font-size: 15px; font-weight: 600; color: #000F24;
      margin-bottom: 4px; line-height: 1.3;
    }
    .legislation-meta { font-size: 12px; color: #777; }
    .year-summary {
      background: #f0f4f7; border-radius: 8px; padding: 12px 16px;
      text-align: center; margin-bottom: 20px; font-size: 14px;
    }
    .cta-section {
      background: linear-gradient(135deg, #f8fafb 0%, #e8f2f6 100%);
      border-radius: 12px; padding: 25px;
      text-align: center; margin-bottom: 20px;
    }
    .cta-button {
      display: inline-block;
      background: linear-gradient(135deg, #BF8F4C 0%, #DEA63B 100%);
      color: #fff; text-decoration: none; padding: 15px 30px;
      border-radius: 30px; font-size: 16px; font-weight: 600;
      margin-top: 15px; transition: all 0.3s ease;
      box-shadow: 0 4px 15px rgba(191, 143, 76, 0.3);
    }
    .cta-button:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(191, 143, 76, 0.4);
    }
    .highlight { color: #BF8F4C; font-weight: 600; }
    .signature-section {
      background: #ffffff; border-radius: 12px;
      padding: 30px 25px; margin: 30px 0 20px;
      border-top: 1px solid #e5e9ec;
    }
    .signature-content { max-width: 400px; }
    .signature-text { font-size: 16px; color: #333; margin: 0 0 15px 0; font-weight: 500; }
    .signature-details { display: flex; align-items: flex-start; gap: 15px; }
    .signature-info { flex: 1; }
    .signature-name { font-size: 18px; font-weight: 700; color: #000F24; margin: 0 0 5px 0; }
    .signature-person { font-size: 16px; color: #BF8F4C; font-weight: 600; margin: 0; }
    .footer {
      background: #f7f9fa; padding: 20px; text-align: center;
      font-size: 13px; color: #888; border-top: 1px solid #e5e9ec;
    }
    .no-legislations { text-align: center; padding: 40px 20px; color: #666; }
    .no-legislations h3 { color: #000F24; margin-bottom: 15px; }
    @media only screen and (max-width: 600px) {
      .container { margin: 10px; border-radius: 12px; }
      .header h1 { font-size: 24px; }
      .stats-number { font-size: 36px; }
      .content { padding: 25px 20px; }
      .cta-section { padding: 20px 15px; }
      .legislation-item { flex-direction: column; align-items: flex-start; gap: 10px; }
      .signature-section { padding: 20px 15px; }
      .signature-details { flex-direction: column; align-items: flex-start; gap: 10px; }
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <div class="header">
      <div class="logo-wrapper">
        <div class="logo-circle">📜</div>
      </div>
      <h1>🏛️ Weekly UAE Legislation Update</h1>
      <p>New legislations delivered to your inbox · {{ target_date }}</p>
    </div>

    <!-- Stats Banner -->
    <div class="stats-banner">
      <div class="stats-number">{{ total_legislations }}</div>
      <div class="stats-text">New Legislation{{ 's' if total_legislations != 1 else '' }} This Week</div>
    </div>

    <!-- Content -->
    <div class="content">
      <p class="intro-text">Hello {{ user_name }}, here's your weekly legislation update! ⚡</p>

      {% if total_legislations > 0 %}
      <p class="description">
        We've scraped and uploaded <span class="highlight">{{ total_legislations }} new UAE legislation{{ 's' if total_legislations != 1 else '' }}</span>
        to our database. All PDFs are indexed and ready for your legal research.
      </p>

      <div class="year-summary">📅 Years covered: {{ year_summary }}</div>

      <!-- Legislation List -->
      <div class="section">
        <div class="section-title">📋 New Legislations ({{ total_legislations }})</div>
        <div class="legislation-list">
          {% for law in all_legislations %}
          <div class="legislation-item">
            <div class="legislation-info">
              <div class="legislation-title">{{ law.title }}</div>
              <div class="legislation-meta">No. {{ law.number }} · {{ law.year }} · <a href="{{ law.url }}" style="color: #BF8F4C;">View on Portal</a></div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      {% else %}
      <!-- No Legislations Section -->
      <div class="no-legislations">
        <h3>📋 No New Legislations This Week</h3>
        <p>No new legislations were published on the UAE legislation portal since our last scan. This could mean:</p>
        <ul style="text-align: left; display: inline-block; margin: 15px 0;">
          <li>No new legislation was published this week</li>
          <li>All available legislations were previously processed</li>
          <li>Temporary issues with the portal</li>
        </ul>
        <p><strong>We'll check again next week!</strong></p>
      </div>
      {% endif %}

      <!-- CTA Section -->
      <div class="cta-section">
        <h3 style="color: #000F24; margin-top: 0;">🚀 Ready to Dive Deeper?</h3>
        <p>Access your LeXi AI dashboard for AI-powered legal research, case analysis, and intelligent search across all legislations.</p>
        <a href="https://www.lexiai.legal" class="cta-button">Open LeXi AI</a>
      </div>

      <p class="description">
        💡 <strong>Pro Tip:</strong> Use our AI-powered LeXi AI Legal Assistant to streamline your research process.
        Get instant access to relevant legislation, case law, and legal insights tailored to your needs.
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
      <p>This automated report contains legislations sourced from the official UAE Legislation portal.</p>
      <p style="margin-top: 10px; font-size: 12px; color: #aaa;">
        Sent to {{ user_email }} · For support, contact our team
      </p>
    </div>
  </div>
</body>
</html>"""

        template = Template(email_template)

        html = template.render(
            user_name=target_name,
            target_date=datetime.now().strftime('%Y-%m-%d'),
            total_legislations=len(new_items),
            all_legislations=all_legislations_for_template,
            year_summary=year_summary,
            current_year=datetime.now().year,
            user_email=target_email,
        )

        FROM_NAME = 'ceo@lexiai.legal'
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{EMAIL_HOST_USER}>"
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
        return False


def broadcast_notifications(new_items: list, run_stats: dict):
    """Fetch users from API and send notification to each."""
    users = get_users_from_api()

    if not users:
        logger.warning("No users returned from API. Skipping broadcast.")
        return

    logger.info("Starting broadcast to %d users...", len(users))
    count = 0
    # for user in users:
    #     if send_email_notification(new_items, run_stats, user_data=user):
    #         count += 1
    #     # Be nice to the SMTP server
    #     time.sleep(1)

    logger.info("Broadcast complete. Sent %d emails.", count)
    print(users)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="UAE Legislation Weekly Crawler – scrape new laws & notify users."
    )
    parser.add_argument(
        "--visible", action="store_true",
        help="Run with visible browser instead of headless.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview new items without downloading (not implemented yet).",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help="Send a test email to verify configuration and exit.",
    )
    parser.add_argument(
        "--email-to",
        help="Override recipient email address for testing.",
    )
    parser.add_argument(
        "--fetch-users", action="store_true",
        help="Fetch and list all users from API then exit.",
    )
    parser.add_argument(
        "--test-broadcast", action="store_true",
        help="Send a TEST email to ALL users fetched from API.",
    )
    args = parser.parse_args()

    # ---- Fetch Users ----
    if args.fetch_users:
        users = get_users_from_api()
        for u in users:
            print(f"  {u.get('name', '?')}  <{u.get('email', '?')}>")
        print(f"\nTotal: {len(users)} users")
        return 0

    # ---- Test Broadcast (All Users) ----
    if args.test_broadcast:
        logger.info("=== TEST BROADCAST MODE ===")
        sample_items = [{
            "title": "[TEST] Federal Decree-Law No. (99) of 2025 on Testing",
            "number": "99",
            "year": "2025",
            "url": "https://uaelegislation.gov.ae/en/legislations/0000",
        }]
        sample_stats = {"downloaded": 1, "skipped": 0, "failed": 0, "total_done": 1}
        users = get_users_from_api()
        if not users:
            logger.error("No users found from API.")
            return 1
        count = 0
        for user in users:
            logger.info("Sending test to %s <%s>", user.get("name"), user.get("email"))
            if send_email_notification(sample_items, sample_stats, user_data=user, debug=True):
                count += 1
            time.sleep(1)
        logger.info("Test broadcast sent to %d/%d users.", count, len(users))
        return 0

    # ---- Single Test Email ----
    if args.test_email:
        target = args.email_to or NOTIFY_EMAIL
        logger.info("Sending test email to %s ...", target)
        sample_items = [{
            "title": "[TEST] Federal Decree-Law No. (99) of 2025 on Testing",
            "number": "99",
            "year": "2025",
            "url": "https://uaelegislation.gov.ae/en/legislations/0000",
        }]
        sample_stats = {"downloaded": 1, "skipped": 0, "failed": 0, "total_done": 1}
        ok = send_email_notification(sample_items, sample_stats, to_email=target, debug=True)
        return 0 if ok else 1

    # ---- Normal Weekly Run ----
    logger.info("=" * 60)
    logger.info("  UAE Legislation – WEEKLY INCREMENTAL CRAWLER")
    logger.info("=" * 60)
    logger.info("  Running in weekly_mode: stops when caught up.")

    result = scrape_legislations(
        headless=not args.visible,
        resume=True,
        weekly_mode=True,
    )

    if isinstance(result, int):
        # Error code returned (e.g. Cloudflare block)
        logger.error("Scraper exited with error code %d", result)
        return result

    new_items = result.get("new_items", [])
    run_stats = result.get("stats", {})
    exit_code = result.get("exit_code", 0)

    logger.info("Scraper finished. Downloaded: %d, Failed: %d",
                run_stats.get("downloaded", 0), run_stats.get("failed", 0))

    # ---- Send Notifications ----
    if new_items:
        logger.info("Sending email notifications for %d new legislations ...", len(new_items))

        if args.email_to:
            send_email_notification(new_items, run_stats, to_email=args.email_to)
        else:
            # Send to all API users
            broadcast_notifications(new_items, run_stats)

            # Also send admin summary
            send_email_notification(new_items, run_stats, to_email=NOTIFY_EMAIL)
    else:
        logger.info("No new legislations found – no emails to send.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
