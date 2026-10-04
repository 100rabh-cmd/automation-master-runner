import os
import time
import json
import logging
import requests
import gspread
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
STATE_FILE = "last_seen_results.json"
CREDENTIALS_FILE = "credentials.json"
SHEET_NAME = "StockPulse Tracker"
RESULTS_TAB = "Results"
WATCHLIST_TAB = "Watchlist"

SCREENER_RESULTS_URL = "https://www.screener.in/announcements/results/"

# Telegram Secrets
TELEGRAM_BOT_TOKEN_RES = os.getenv("TELEGRAM_BOT_TOKEN_RES")
TELEGRAM_CHAT_ID_RES = os.getenv("TELEGRAM_CHAT_ID_RES")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_watchlist(gc):
    """Fetches active stocks and their metrics from Watchlist tab for metadata enrichment."""
    try:
        sh = gc.open(SHEET_NAME)
        ws = sh.worksheet(WATCHLIST_TAB)
        records = ws.get_all_records()
        
        watchlist = {}
        for r in records:
            active_flag = str(r.get('Active', 'yes')).strip().lower()
            if active_flag in ['yes', 'true', '1']:
                company_name = str(r.get('Stock Name', r.get('Company Name', ''))).strip().lower()
                ticker = str(r.get('Ticker', '')).strip()
                clean_code = ticker.replace("BOM:", "").replace("NSE:", "").strip()
                
                if company_name:
                    watchlist[company_name] = {
                        "company": str(r.get('Stock Name', r.get('Company Name', 'Unknown'))).strip(),
                        "ticker_formatted": f"BOM:{clean_code}" if clean_code.isdigit() else ticker,
                        "price": str(r.get('Current Price', 'N/A')).strip(),
                        "mcap": str(r.get('Market Cap (Cr)', 'N/A')).strip(),
                        "pe": str(r.get('P/E Ratio', 'N/A')).strip()
                    }
        return watchlist
    except Exception as e:
        logging.error(f"Error reading Watchlist tab: {e}")
        return {}

def fetch_screener_results():
    """Scrapes financial results announcements directly from Screener."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    
    try:
        res = requests.get(SCREENER_RESULTS_URL, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        
        announcements = []
        
        # Screener announcement rows are structured inside list items, table rows, or cards
        rows = soup.find_all('li', class_=lambda c: c and 'announcement' in c.lower()) or \
               soup.find_all('tr') or \
               soup.find_all('div', class_='card')

        for row in rows:
            company_elem = row.find('a', href=lambda h: h and '/company/' in h)
            if not company_elem:
                continue

            company_name = company_elem.get_text(strip=True)
            
            # Extract PDF / Document link
            pdf_elem = row.find('a', href=lambda h: h and ('.pdf' in h.lower() or 'bseindia' in h.lower() or 'attachment' in h.lower()))
            pdf_url = ""
            if pdf_elem:
                pdf_url = pdf_elem['href']
                if pdf_url.startswith('/'):
                    pdf_url = f"https://www.screener.in{pdf_url}"

            # Extract headline text
            headline_elem = row.find('div', class_='text') or row.find('p')
            if headline_elem:
                headline = headline_elem.get_text(" ", strip=True)
            else:
                headline = row.get_text(" ", strip=True)
                headline = headline.replace(company_name, "").strip()

            unique_key = pdf_url if pdf_url else f"{company_name}_{headline[:50]}"

            announcements.append({
                "company": company_name,
                "headline": headline,
                "pdf_url": pdf_url,
                "unique_key": unique_key
            })
            
        return announcements
    except Exception as e:
        logging.error(f"Failed to fetch results from Screener: {e}")
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
        logging.warning("Telegram skipped: TELEGRAM_BOT_TOKEN_RES or TELEGRAM_CHAT_ID_RES not set.")
        return

    text = (
        f"📊 <b>New Financial Results Alert!</b>\n\n"
        f"📌 <b>Company:</b> {company} (<code>{ticker}</code>)\n\n"
        f"📝 <b>Headline:</b> {title}\n\n"
        f"📄 <b>PDF Document:</b> {pdf_link if pdf_link else 'N/A'}\n\n"
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

    # Hydrate seen_ids from Google Sheet history
    try:
        recent_rows = results_sheet.get_all_values()
        for row in recent_rows[1:150]:
            if len(row) >= 4 and row[3].startswith("http"):
                seen_ids.add(row[3].strip())
    except Exception as e:
        logging.warning(f"Could not read historical sheet rows: {e}")

    logging.info("Scanning Screener for Financial Results...")
    announcements = fetch_screener_results()

    for ann in announcements:
        unique_key = ann['unique_key']
        if unique_key in seen_ids:
            continue

        company_raw = ann['company']
        headline = ann['headline']
        pdf_url = ann['pdf_url']

        matched_info = watchlist.get(company_raw.lower(), {
            "company": company_raw,
            "ticker_formatted": company_raw,
            "price": "N/A",
            "mcap": "N/A",
            "pe": "N/A"
        })

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        price_formula = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "price"), "N/A")'
        mcap_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "marketcap")/10000000, "N/A")'
        pe_formula    = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "pe"), "N/A")'
        high_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "high52"), "N/A")'
        low_formula   = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "low52"), "N/A")'

        results_sheet.insert_row([
            current_time,
            matched_info['company'],
            headline,
            pdf_url,
            "READY",
            matched_info['ticker_formatted'],
            price_formula,
            mcap_formula,
            pe_formula,
            high_formula,
            low_formula
        ], index=2, value_input_option="USER_ENTERED")

        send_telegram_alert(
            company=matched_info['company'],
            ticker=matched_info['ticker_formatted'],
            title=headline,
            pdf_link=pdf_url
        )

        seen_ids.add(unique_key)
        save_last_seen(seen_ids)
        time.sleep(1)

if __name__ == "__main__":
    run_results_tracker()
