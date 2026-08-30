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
STATE_FILE = "last_seen_expansions.json"
CREDENTIALS_FILE = "credentials.json"
SHEET_NAME = "StockPulse Tracker"
EXPANSION_TAB = "Expansion"
WATCHLIST_TAB = "Watchlist"

TELEGRAM_BOT_TOKEN_CE = os.getenv("TELEGRAM_BOT_TOKEN_CE")
TELEGRAM_CHAT_ID_CE = os.getenv("TELEGRAM_CHAT_ID_CE")

# Expansion Keywords to match BSE Announcement Headlines & Summaries
EXPANSION_KEYWORDS = [
    "expansion", "capacity", "commercial production",
    "commissioning", "new plant", "new facility",
    "setting up", "capacity addition", "greenfield", "brownfield"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_watchlist(gc):
    """Fetches active stocks and their cached metrics from Watchlist tab."""
    try:
        sh = gc.open(SHEET_NAME)
        ws = sh.worksheet(WATCHLIST_TAB)
        records = ws.get_all_records()
        
        watchlist = {}
        for r in records:
            active_flag = str(r.get('Active', 'yes')).strip().lower()
            if active_flag in ['yes', 'true', '1']:
                ticker = str(r.get('Ticker', '')).strip()
                clean_code = ticker.replace("BOM:", "").replace("NSE:", "").strip()
                
                if clean_code.isdigit():
                    watchlist[clean_code] = {
                        "company": str(r.get('Stock Name', r.get('Company Name', 'Unknown'))).strip(),
                        "ticker_formatted": f"BOM:{clean_code}",
                        "price": str(r.get('Current Price', 'N/A')).strip(),
                        "mcap": str(r.get('Market Cap (Cr)', 'N/A')).strip(),
                        "pe": str(r.get('P/E Ratio', 'N/A')).strip()
                    }
        return watchlist
    except Exception as e:
        logging.error(f"Error reading Watchlist tab: {e}")
        return {}

def fetch_bse_announcements(scrip_cd):
    """Fetches announcements directly from BSE API for a specific scrip code."""
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
        logging.error(f"Failed to fetch BSE announcements for scrip {scrip_cd}: {e}")
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

def send_telegram_alert(company, ticker, title, pdf_link, price="N/A", mcap="N/A", pe="N/A"):
    if not TELEGRAM_BOT_TOKEN_CE or not TELEGRAM_CHAT_ID_CE:
        logging.warning("Telegram skipped: Bot token or chat ID not set.")
        return

    snapshot = f"• <b>Price:</b> ₹{price} | <b>Mcap:</b> ₹{mcap} Cr | <b>P/E:</b> {pe}\n\n" if price != "N/A" else ""

    text = (
        f"🏭 <b>New Capacity Expansion Alert!</b>\n\n"
        f"📌 <b>Company:</b> {company} (<code>{ticker}</code>)\n\n"
        f"📊 <b>Stock Snapshot:</b>\n{snapshot}"
        f"📝 <b>Headline:</b> {title}\n\n"
        f"📄 <b>PDF Document:</b> {pdf_link}\n\n"
        f"📲 Follow: @financewith100rabh"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_CE}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID_CE,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def run_expansion_tracker():
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open(SHEET_NAME)
    expansion_sheet = sh.worksheet(EXPANSION_TAB)

    watchlist = get_watchlist(gc)
    seen_ids = load_last_seen()

    # Hydrate seen_ids from Google Sheet history
    try:
        recent_rows = expansion_sheet.get_all_values()
        for row in recent_rows[1:150]:
            if len(row) >= 4 and row[3].startswith("http"):
                seen_ids.add(row[3].strip())
    except Exception as e:
        logging.warning(f"Could not read historical sheet rows: {e}")

    logging.info(f"Scanning Capacity Expansions for {len(watchlist)} stocks via BSE API...")

    for scrip_cd, info in watchlist.items():
        announcements = fetch_bse_announcements(scrip_cd)
        for ann in announcements:
            headline = str(ann.get('HEADLINE', '')).strip()
            news_sub = str(ann.get('NEWSSUB', '')).strip()
            combined_text = f"{news_sub} {headline}".lower()

            # Keyword filtering for capacity expansion filings
            if not any(k in combined_text for k in EXPANSION_KEYWORDS):
                continue

            attachment = ann.get('ATTACHMENTNAME')
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
            unique_key = pdf_url if pdf_url else f"{scrip_cd}_{headline}"

            if unique_key in seen_ids:
                continue

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            price_formula = '=IFERROR(GOOGLEFINANCE(F2, "price"), "N/A")'
            mcap_formula  = '=IFERROR(GOOGLEFINANCE(F2, "marketcap")/10000000, "N/A")'
            pe_formula    = '=IFERROR(GOOGLEFINANCE(F2, "pe"), "N/A")'
            high_formula  = '=IFERROR(GOOGLEFINANCE(F2, "high52"), "N/A")'
            low_formula   = '=IFERROR(GOOGLEFINANCE(F2, "low52"), "N/A")'

            expansion_sheet.insert_row([
                current_time,
                info['company'],
                headline,
                pdf_url,
                "READY",
                info['ticker_formatted'],
                price_formula,
                mcap_formula,
                pe_formula,
                high_formula,
                low_formula
            ], index=2, value_input_option="USER_ENTERED")

            send_telegram_alert(
                company=info['company'],
                ticker=info['ticker_formatted'],
                title=headline,
                pdf_link=pdf_url,
                price=info['price'],
                mcap=info['mcap'],
                pe=info['pe']
            )

            seen_ids.add(unique_key)
            save_last_seen(seen_ids)
            time.sleep(1)

if __name__ == "__main__":
    run_expansion_tracker()
