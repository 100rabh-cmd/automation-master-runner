import os
from dotenv import load_dotenv
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import gspread
import time
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# --- CONFIGURATION ---
SCREENER_URL = "https://www.screener.in/announcements/user-filters/223295/"
STATE_FILE = "last_seen_expansions.json"
CREDENTIALS_FILE = "credentials.json"
SHEET_NAME = "StockPulse Tracker"
EXPANSION_TAB = "Expansion"
WATCHLIST_TAB = "Watchlist"

TELEGRAM_BOT_TOKEN_CE = os.getenv("TELEGRAM_BOT_TOKEN_CE")
TELEGRAM_CHAT_ID_CE = os.getenv("TELEGRAM_CHAT_ID_CE")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": "csrftoken=PNaWmraZrRgc9NfKH57aPQhp3ngDTVt9; sessionid=k8wmkhm9isrfjj64sivgr4gl11k5b4s5"
}

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

def get_watchlist_metrics():
    """Fetches pre-calculated Stock Metrics directly from Watchlist tab."""
    metrics_map = {}
    try:
        sheet = init_google_sheet(WATCHLIST_TAB)
        records = sheet.get_all_records()
        for r in records:
            ticker = str(r.get('Ticker', '')).strip().upper()
            stock_name = str(r.get('Stock Name', r.get('Company Name', ''))).strip().upper()
            
            data = {
                "price": str(r.get('Current Price', 'N/A')).strip(),
                "mcap": str(r.get('Market Cap (Cr)', 'N/A')).strip(),
                "pe": str(r.get('P/E Ratio', 'N/A')).strip()
            }
            if ticker:
                metrics_map[ticker] = data
                metrics_map[ticker.replace("BOM:", "").replace("NSE:", "")] = data
            if stock_name:
                metrics_map[stock_name] = data
    except Exception as e:
        print(f"Watchlist lookup notice: {e}")
    return metrics_map

def fetch_bse_live_metrics(scrip_code):
    """Fallback fetch directly from BSE API for SME/untracked stocks."""
    clean_code = str(scrip_code).replace("BOM:", "").replace("NSE:", "").strip()
    price, mcap, pe = "N/A", "N/A", "N/A"
    
    if not clean_code.isdigit():
        return price, mcap, pe

    try:
        url_hdr = f"https://api.bseindia.com/BseIndiaAPI/api/GetScripHeaderData/w?scripcode={clean_code}"
        res_hdr = requests.get(url_hdr, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/"}, timeout=5)
        if res_hdr.status_code == 200:
            hdr_data = res_hdr.json().get("Header", {})
            price = str(hdr_data.get("LTP", hdr_data.get("PrevClose", "N/A")))
            mcap = str(hdr_data.get("McapFull", "N/A"))
            pe = str(hdr_data.get("PE", "N/A"))
    except Exception:
        pass

    return price, mcap, pe

def send_telegram_alert(company, ticker, title, pdf_link, price="N/A", mcap="N/A", pe="N/A"):
    if not TELEGRAM_BOT_TOKEN_CE or TELEGRAM_BOT_TOKEN_CE == "YOUR_TELEGRAM_BOT_TOKEN_CE":
        print("Telegram skipped: Bot token not configured.")
        return
    
    ticker_display = f" (`{ticker}`)" if ticker else ""
    
    snapshot_block = (
        f"📊 *Stock Snapshot:*\n"
        f"• *Price:* ₹{price} | *Mcap:* ₹{mcap} Cr | *P/E:* {pe}\n\n"
    )

    message = (
        f"🏭 *New Capacity Expansion Alert!*\n\n"
        f"📌 *Company:* {company}{ticker_display}\n\n"
        f"{snapshot_block}"
        f"📝 *Details:* {title}\n\n"
        f"📄 *PDF Document:* {pdf_link}\n\n"
        f"📲 Follow: @financewith100rabh"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_CE}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID_CE,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"Telegram alert sent successfully for {company}.")
        else:
            print(f"Failed to send Telegram alert: {response.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def normalize_details(text):
    """Removes dynamic relative date prefixes (e.g. 'Today ', 'Yesterday ', '7 Aug ago ')."""
    cleaned = re.sub(r'^(Today|Yesterday|[A-Za-z]{3}\s+\d{1,2},\s+\d{4}|\d+\s+[A-Za-z]+\s+ago)\s*', '', text, flags=i) if (i := re.IGNORECASE) else text
    return re.sub(r'\s+', ' ', cleaned).strip()

def fetch_expansion_announcements():
    print(f"[{datetime.now()}] Fetching updates from Screener filter...")
    try:
        response = requests.get(SCREENER_URL, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            print(f"Failed to fetch page. Status code: {response.status_code}")
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

            # Primary Deduplication Key: PDF Link (100% unique per filing)
            if pdf_link and "screener.in" not in pdf_link:
                unique_key = pdf_link.strip()
            else:
                clean_text = normalize_details(text_block)
                unique_key = f"{company_name.upper()}_{clean_text[:120]}"

            if unique_key in seen_in_page:
                continue
            seen_in_page.add(unique_key)

            updates.append({
                "company": company_name,
                "ticker": formatted_ticker,
                "raw_ticker": raw_ticker,
                "details": text_block[:300],
                "pdf_link": pdf_link if pdf_link else SCREENER_URL,
                "signature": unique_key
            })

        return updates
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []

def run_automation():
    seen_ids = load_last_seen()
    watchlist_metrics = get_watchlist_metrics()
    
    # Read existing PDF Links from Sheet to prevent historical duplicates
    try:
        expansion_sheet = init_google_sheet(EXPANSION_TAB)
        recent_rows = expansion_sheet.get_all_values()
        for row in recent_rows[1:150]:
            if len(row) >= 4:
                pdf_val = row[3].strip()
                if pdf_val and pdf_val.startswith("http"):
                    seen_ids.add(pdf_val)
                comp_val = row[1].strip().upper()
                det_val = normalize_details(row[2])[:120]
                seen_ids.add(f"{comp_val}_{det_val}")
    except Exception as e:
        print(f"Warning reading sheet: {e}")

    current_updates = fetch_expansion_announcements()
    if not current_updates:
        print("No updates parsed.")
        return

    new_items_to_add = []
    for item in current_updates:
        if item['signature'] in seen_ids:
            continue
        new_items_to_add.append(item)

    if new_items_to_add:
        print(f"\nFound {len(new_items_to_add)} new announcements to sync...")
        for item in reversed(new_items_to_add):
            print(f"\n🚨 NEW EXPANSION FOUND:\n- Company: {item['company']}\n- Ticker: {item['ticker']}")
            
            # Lookup Price, Mcap, and PE from Watchlist, fallback to BSE API
            comp_key = item['company'].strip().upper()
            tick_key = item['ticker'].strip().upper()
            
            stock_info = watchlist_metrics.get(tick_key) or watchlist_metrics.get(comp_key)
            if stock_info and stock_info.get("price") != "N/A":
                price = stock_info["price"]
                mcap = stock_info["mcap"]
                pe = stock_info["pe"]
            else:
                price, mcap, pe = fetch_bse_live_metrics(item['raw_ticker'])

            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Formulas for Google Sheets
            price_formula = '=IFERROR(GOOGLEFINANCE(F2, "price"), "N/A")'
            mcap_formula  = '=IFERROR(GOOGLEFINANCE(F2, "marketcap")/10000000, "N/A")'
            pe_formula    = '=IFERROR(GOOGLEFINANCE(F2, "pe"), "N/A")'
            high_formula  = '=IFERROR(GOOGLEFINANCE(F2, "high52"), "N/A")'
            low_formula   = '=IFERROR(GOOGLEFINANCE(F2, "low52"), "N/A")'

            expansion_sheet.insert_row([
                current_time, 
                item['company'], 
                item['details'], 
                item['pdf_link'], 
                "READY",
                item['ticker'],
                price_formula,
                mcap_formula,
                pe_formula,
                high_formula,
                low_formula
            ], index=2, value_input_option="USER_ENTERED")
            
            send_telegram_alert(
                item['company'], 
                item['ticker'], 
                item['details'], 
                item['pdf_link'],
                price=price,
                mcap=mcap,
                pe=pe
            )
            
            seen_ids.add(item['signature'])
            save_last_seen(seen_ids)
            time.sleep(2) 
        
        print("\nSynced successfully.")
    else:
        print("\nNo new capacity updates found.")

if __name__ == "__main__":
    run_automation()