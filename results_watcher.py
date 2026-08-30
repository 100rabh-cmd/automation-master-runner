import os
import time
import json
import logging
import requests
import gspread
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
STATE_FILE = "last_seen_results.json"
CREDENTIALS_FILE = "credentials.json"
SHEET_NAME = "StockPulse Tracker"
RESULTS_TAB = "Results"

TELEGRAM_BOT_TOKEN_RES = os.getenv("TELEGRAM_BOT_TOKEN_RES")
TELEGRAM_CHAT_ID_RES = os.getenv("TELEGRAM_CHAT_ID_RES")

RESULT_KEYWORDS = ["financial results", "financial result", "board meeting outcome - financial results"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_watchlist(gc):
    try:
        sh = gc.open(SHEET_NAME)
        ws = sh.worksheet("Watchlist")
        records = ws.get_all_records()
        watchlist = {}
        for r in records:
            if str(r.get('Active', '')).strip().lower() == 'yes':
                ticker = str(r.get('Ticker', '')).strip()
                if ticker.isdigit():
                    watchlist[ticker] = str(r.get('Stock Name', r.get('Company Name', 'Unknown'))).strip()
        return watchlist
    except Exception as e:
        logging.error(f"Error reading Watchlist: {e}")
        return {}

def fetch_bse_results_for_scrip(scrip_cd):
    url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip={scrip_cd}&strSearch=P&strToDate=&strType=C"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.bseindia.com/',
        'Accept': 'application/json, text/plain, */*'
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return res.json().get("Table", [])
    except Exception as e:
        logging.error(f"Failed to fetch BSE data for {scrip_cd}: {e}")
        return []

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
        json.dump(list(seen_set)[-800:], f)

def send_telegram_alert(company, ticker, title, pdf_link):
    if not TELEGRAM_BOT_TOKEN_RES or not TELEGRAM_CHAT_ID_RES:
        return

    text = (
        f"📊 <b>New Financial Results Alert!</b>\n\n"
        f"<b>Company:</b> {company} (<code>{ticker}</code>)\n\n"
        f"<b>Headline:</b> {title}\n\n"
        f"📄 <b>PDF Document:</b> {pdf_link}\n\n"
        f"📲 Follow: @financewith100rabh"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_RES}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID_RES,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def run_results_tracker():
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open(SHEET_NAME)
    results_sheet = sh.worksheet(RESULTS_TAB)

    watchlist = get_watchlist(gc)
    seen_ids = load_last_seen()
    
    logging.info(f"Scanning Results for {len(watchlist)} watchlist stocks via direct BSE API...")

    for scrip_cd, company_name in watchlist.items():
        announcements = fetch_bse_results_for_scrip(scrip_cd)
        for ann in announcements:
            headline = str(ann.get('HEADLINE', '')).strip()
            news_sub = str(ann.get('NEWSSUB', '')).strip()
            combined_text = f"{news_sub} {headline}".lower()

            # Filter for financial results only
            if not any(k in combined_text for k in RESULT_KEYWORDS):
                continue

            attachment = ann.get('ATTACHMENTNAME')
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
            unique_key = pdf_url if pdf_url else f"{scrip_cd}_{headline}"

            if unique_key in seen_ids:
                continue

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            formatted_ticker = f"BOM:{scrip_cd}"

            price_formula = '=IFERROR(GOOGLEFINANCE(F2, "price"), "N/A")'
            mcap_formula  = '=IFERROR(GOOGLEFINANCE(F2, "marketcap")/10000000, "N/A")'
            pe_formula    = '=IFERROR(GOOGLEFINANCE(F2, "pe"), "N/A")'
            high_formula  = '=IFERROR(GOOGLEFINANCE(F2, "high52"), "N/A")'
            low_formula   = '=IFERROR(GOOGLEFINANCE(F2, "low52"), "N/A")'

            results_sheet.insert_row([
                current_time, company_name, headline, pdf_url, "READY",
                formatted_ticker, price_formula, mcap_formula, pe_formula, high_formula, low_formula
            ], index=2, value_input_option="USER_ENTERED")

            send_telegram_alert(company_name, formatted_ticker, headline, pdf_url)

            seen_ids.add(unique_key)
            save_last_seen(seen_ids)
            time.sleep(1)

if __name__ == "__main__":
    run_results_tracker()
