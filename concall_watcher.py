import os
import sys
import json
import re
import time
import html
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
SCREENER_CONCALL_URL = "https://www.screener.in/announcements/user-filters/223297/"
STATE_FILE = "last_seen_concalls.json"
CREDENTIALS_FILE = "credentials.json"
SHEET_NAME = "StockPulse Tracker"
CONCALL_TAB = "Concalls"

TELEGRAM_BOT_TOKEN_CC = os.getenv("TELEGRAM_BOT_TOKEN_CC") or os.getenv("TELEGRAM_BOT_TOKEN_ANN") or os.getenv("TELEGRAM_BOT_TOKEN_CE")
TELEGRAM_CHAT_ID_CC = os.getenv("TELEGRAM_CHAT_ID_CC") or os.getenv("TELEGRAM_CHAT_ID_ANN") or os.getenv("TELEGRAM_CHAT_ID_CE")

SCREENER_SESSION_ID = os.getenv("SCREENER_SESSION_ID", "k8wmkhm9isrfjj64sivgr4gl11k5b4s5")
SCREENER_CSRF_TOKEN = os.getenv("SCREENER_CSRF_TOKEN", "PNaWmraZrRgc9NfKH57aPQhp3ngDTVt9")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": f"csrftoken={SCREENER_CSRF_TOKEN}; sessionid={SCREENER_SESSION_ID}"
}

def send_session_alert(reason: str):
    """Notifies Telegram if the Screener session token has expired or is invalid."""
    if not TELEGRAM_BOT_TOKEN_CC or not TELEGRAM_CHAT_ID_CC:
        return

    message = (
        f"🚨 <b>SCREENER SESSION EXPIRED / FAILED</b> 🚨\n\n"
        f"<b>Reason:</b> {reason}\n\n"
        f"🔑 <b>Action Required:</b> Log in to Screener.in, retrieve a new <code>sessionid</code> cookie, "
        f"and update your GitHub repository secret <code>SCREENER_SESSION_ID</code>."
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_CC}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID_CC, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send session alert: {e}")

def init_google_sheet(tab_name):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).worksheet(tab_name)

def load_last_seen():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_last_seen(seen_set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen_set)[-500:], f)

def format_ticker(symbol_or_code):
    code = str(symbol_or_code).strip().upper()
    if code.isdigit():
        return f"BOM:{code}"
    elif code and not (code.startswith("BOM:") or code.startswith("NSE:")):
        return f"NSE:{code}"
    return code

def send_telegram_alert(company, ticker, title, pdf_link):
    if not TELEGRAM_BOT_TOKEN_CC:
        return

    ticker_display = f" (<code>{html.escape(ticker)}</code>)" if ticker else ""
    message = (
        f"🎙️ <b>New Concall / Investor Presentation Alert!</b>\n\n"
        f"📌 <b>Company:</b> {html.escape(company)}{ticker_display}\n\n"
        f"📝 <b>Details:</b> {html.escape(title)}\n\n"
        f"📄 <b>PDF Document:</b> {pdf_link}\n\n"
        f"📲 Follow: @financewith100rabh"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_CC}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID_CC, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert error: {e}")

def normalize_details(text):
    cleaned = re.sub(r'^(Today|Yesterday|[A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d+\s+[A-Za-z]+\s+ago)\s*', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', cleaned).strip()

def fetch_concall_announcements():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fetching concall updates from Screener...")
    try:
        response = requests.get(SCREENER_CONCALL_URL, headers=HEADERS, timeout=15)

        is_redirected_to_login = "login" in response.url.lower()
        contains_login_form = "Log in" in response.text and 'name="username"' in response.text

        if response.status_code in [401, 403] or is_redirected_to_login or contains_login_form:
            reason = "Screener session expired or login required."
            print(f"❌ {reason}")
            send_session_alert(reason)
            return []

        if response.status_code != 200:
            reason = f"Screener HTTP error status code: {response.status_code}"
            print(f"❌ {reason}")
            send_session_alert(reason)
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        updates = []
        seen_in_page = set()

        cards = soup.find_all(['div', 'article', 'li'], class_=lambda x: x and ('card' in x or 'announcement' in x))
        if not cards:
            cards = soup.find_all('div', class_='flex')

        for card in cards:
            company_elem = card.find('a', href=lambda x: x and '/company/' in x)
            if not company_elem:
                continue
            company_name = company_elem.get_text(strip=True)

            href = company_elem.get('href', '')
            href_parts = [p for p in href.split('/') if p]
            raw_ticker = ""
            if 'company' in href_parts:
                idx = href_parts.index('company')
                if idx + 1 < len(href_parts):
                    raw_ticker = href_parts[idx + 1].strip()

            formatted_ticker = format_ticker(raw_ticker)
            text_block = card.get_text(separator=" ", strip=True)
            
            pdf_link = ""
            link_elem = card.find('a', href=lambda x: x and ('.pdf' in x or 'announcements' in x))
            if link_elem:
                h = link_elem['href']
                pdf_link = h if h.startswith('http') else "https://www.screener.in" + h

            unique_key = pdf_link.strip() if (pdf_link and "screener.in" not in pdf_link) else f"{company_name.upper()}_{normalize_details(text_block)[:120]}"

            if unique_key in seen_in_page:
                continue
            seen_in_page.add(unique_key)

            updates.append({
                "company": company_name,
                "ticker": formatted_ticker,
                "details": text_block[:300],
                "pdf_link": pdf_link if pdf_link else SCREENER_CONCALL_URL,
                "signature": unique_key
            })

        return updates
    except Exception as e:
        err_str = f"Network Exception: {str(e)}"
        print(f"Error fetching concalls: {err_str}")
        send_session_alert(err_str)
        return []

def run_automation():
    seen_ids = load_last_seen()

    try:
        concall_sheet = init_google_sheet(CONCALL_TAB)
        recent_rows = concall_sheet.get_all_values()
        for row in recent_rows[1:150]:
            if len(row) >= 4:
                pdf_val = row[3].strip()
                if pdf_val and pdf_val.startswith("http"):
                    seen_ids.add(pdf_val)
                comp_val = row[1].strip().upper()
                det_val = normalize_details(row[2])[:120]
                seen_ids.add(f"{comp_val}_{det_val}")
    except Exception as e:
        print(f"Warning reading Concalls sheet: {e}")

    current_updates = fetch_concall_announcements()
    if not current_updates:
        print("No concall updates parsed.")
        return

    new_items_to_add = [item for item in current_updates if item['signature'] not in seen_ids]

    if new_items_to_add:
        print(f"\nFound {len(new_items_to_add)} new concall announcements...")
        for item in reversed(new_items_to_add):
            print(f"\n🚨 NEW CONCALL FOUND:\n- Company: {item['company']}\n- Ticker: {item['ticker']}")

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            price_formula = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "price"), "N/A")'
            mcap_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "marketcap")/10000000, "N/A")'
            pe_formula    = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "pe"), "N/A")'
            high_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "high52"), "N/A")'
            low_formula   = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "low52"), "N/A")'

            concall_sheet.insert_row([
                current_time, item['company'], item['details'], item['pdf_link'],
                "READY", item['ticker'], price_formula, mcap_formula, pe_formula, high_formula, low_formula
            ], index=2, value_input_option="USER_ENTERED")

            send_telegram_alert(item['company'], item['ticker'], item['details'], item['pdf_link'])
            seen_ids.add(item['signature'])
            save_last_seen(seen_ids)
            time.sleep(2)
        print("\nConcalls synced successfully.")
    else:
        print("\nNo new concall updates found.")

if __name__ == "__main__":
    run_automation()
