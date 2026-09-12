import os
import time
import json
import logging
import warnings
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import gspread
from gspread.exceptions import APIError

warnings.filterwarnings("ignore")
import os
import time
import json
import logging
import warnings
import re
import html
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from oauth2client.service_account import ServiceAccountCredentials

warnings.filterwarnings("ignore")
load_dotenv()

# ==============================================================================
# ------------------------- CONFIGURATION HEADER -------------------------------
# ==============================================================================

CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_NAME = "StockPulse Tracker"
STATE_FILE = "last_seen_master.json"

SCREENER_CONCALL_URL = "https://www.screener.in/announcements/user-filters/223297/"

# Telegram Environment Variables
TELEGRAM_BOT_TOKEN_ANN = os.getenv("TELEGRAM_BOT_TOKEN_ANN")
TELEGRAM_CHAT_ID_ANN = os.getenv("TELEGRAM_CHAT_ID_ANN")

TELEGRAM_BOT_TOKEN_RES = os.getenv("TELEGRAM_BOT_TOKEN_RES")
TELEGRAM_CHAT_ID_RES = os.getenv("TELEGRAM_CHAT_ID_RES")

TELEGRAM_BOT_TOKEN_CE = os.getenv("TELEGRAM_BOT_TOKEN_CE")
TELEGRAM_CHAT_ID_CE = os.getenv("TELEGRAM_CHAT_ID_CE")

TELEGRAM_BOT_TOKEN_CC = os.getenv("TELEGRAM_BOT_TOKEN_CC")
TELEGRAM_CHAT_ID_CC = os.getenv("TELEGRAM_CHAT_ID_CC")

BSE_PROXY_URL = os.getenv("BSE_PROXY_URL") or os.getenv("HTTPS_PROXY")

# Categorization Keywords
EXPANSION_KEYWORDS = [
    "expansion", "capacity", "commercial production", "commissioning",
    "new plant", "new facility", "setting up", "capacity addition", 
    "greenfield", "brownfield", "award_of_order_receipt_of_order", 
    "award of order", "receipt of order", "incorporation of subsidiary",
    "bonus / stock split / rights issue", "buyback", "fund raising", "issue of securities"
]

RESULT_KEYWORDS = [
    "financial results", "financial result", "board meeting outcome - financial results",
    "audited result", "unaudited result", "quarterly result"
]

CONCALL_KEYWORDS = [
    "analyst / investor meet - outcome", "earnings call transcript", 
    "investor presentation", "analyst presentation", "audio recording", "concall"
]

NOISE_KEYWORDS = [
    "trading window", "share certificate", "loss of share", "duplicate share",
    "compliance certificate", "newspaper publication", "clarification", 
    "voting results", "scrutinizer report", "loss of certificate", 
    "substantial acquisition", "takeovers", "regulation 29", "regulation 10", 
    "reg 29", "reg 10", "reg 29(2)", "reg 10(6)", "sast"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==============================================================================
# ------------------------- MASTER AUTOMATION ENGINE ---------------------------
# ==============================================================================

class MasterAutomationEngine:
    def __init__(self):
        self.gc = self._connect_sheets_with_retry()
        self.sh = self.gc.open(GOOGLE_SHEET_NAME)
        self.session = self._init_bse_session()
        self.seen_ids = self.load_seen_ids()

    def _connect_sheets_with_retry(self, max_retries=5):
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        for attempt in range(1, max_retries + 1):
            try:
                creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
                return gspread.authorize(creds)
            except Exception as e:
                if attempt == max_retries:
                    raise e
                time.sleep(attempt * 3)

    def _init_bse_session(self) -> requests.Session:
        session = requests.Session()
        if BSE_PROXY_URL:
            session.proxies = {"http": BSE_PROXY_URL, "https": BSE_PROXY_URL}

        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://www.bseindia.com/',
            'Accept': 'application/json, text/plain, */*'
        })
        return session

    def load_seen_ids(self) -> set:
        seen = set()
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    seen = set(json.load(f))
            except Exception:
                pass

        # Hydrate seen IDs from sheet tabs
        for tab_name in ["Expansion", "Results", "Concalls", "Log"]:
            try:
                ws = self.sh.worksheet(tab_name)
                rows = ws.get_all_values()
                for r in rows[1:150]:
                    if len(r) >= 4:
                        if r[3].strip().startswith("http"):
                            seen.add(r[3].strip())
                        if len(r) >= 3:
                            seen.add(f"{r[1].strip().upper()}_{r[2].strip()[:100]}")
            except Exception:
                continue
        return seen

    def save_seen_ids(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(list(self.seen_ids)[-1000:], f)
        except Exception as e:
            logging.error(f"Error saving state: {e}")

    def get_watchlist(self) -> dict:
        try:
            ws = self.sh.worksheet("Watchlist")
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
            logging.error(f"Error loading Watchlist: {e}")
            return {}

    def fetch_bse_announcements(self, scrip_cd: str) -> list:
        url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip={scrip_cd}&strSearch=P&strToDate=&strType=C"
        try:
            res = self.session.get(url, timeout=10)
            res.raise_for_status()
            return res.json().get("Table", [])
        except Exception as e:
            logging.error(f"Failed to fetch BSE data for {scrip_cd}: {e}")
            return []

    def fetch_screener_concalls(self) -> list:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(SCREENER_CONCALL_URL, headers=headers, timeout=15)
            if res.status_code != 200:
                return []
            
            soup = BeautifulSoup(res.text, 'html.parser')
            cards = soup.find_all(['div', 'article', 'li'], class_=lambda x: x and ('card' in x or 'announcement' in x))
            if not cards:
                cards = soup.find_all('div', class_='flex')

            updates = []
            for card in cards:
                comp = card.find('a', href=lambda x: x and '/company/' in x)
                if not comp:
                    continue
                c_name = comp.get_text(strip=True)
                href = comp.get('href', '')
                parts = [p for p in href.split('/') if p]
                raw_ticker = parts[parts.index('company') + 1] if 'company' in parts and parts.index('company') + 1 < len(parts) else ""
                
                ticker_fmt = f"BOM:{raw_ticker}" if raw_ticker.isdigit() else (f"NSE:{raw_ticker}" if raw_ticker else "")
                details = card.get_text(separator=" ", strip=True)[:300]
                
                pdf_elem = card.find('a', href=lambda x: x and ('.pdf' in x or 'announcements' in x))
                pdf_link = pdf_elem['href'] if pdf_elem else ""
                if pdf_link and not pdf_link.startswith('http'):
                    pdf_link = "https://www.screener.in" + pdf_link

                unique_key = pdf_link if pdf_link else f"{c_name.upper()}_{details[:100]}"
                updates.append({
                    "company": c_name,
                    "ticker": ticker_fmt,
                    "headline": details,
                    "pdf_url": pdf_link or SCREENER_CONCALL_URL,
                    "unique_key": unique_key,
                    "target_tab": "Concalls",
                    "route_group": "CC"
                })
            return updates
        except Exception as e:
            logging.error(f"Error scraping Screener concalls: {e}")
            return []

    def classify_announcement(self, text: str) -> tuple[str, str]:
        t = text.lower()
        if any(k in t for k in NOISE_KEYWORDS):
            return None, None
        
        if any(k in t for k in EXPANSION_KEYWORDS):
            return "Expansion", "CE"
        if any(k in t for k in RESULT_KEYWORDS):
            return "Results", "RES"
        if any(k in t for k in CONCALL_KEYWORDS):
            return "Concalls", "CC"
        
        return "Log", "ANN"

    def send_telegram_alert(self, company: str, ticker: str, headline: str, pdf_url: str, route_group: str, stock_info: dict = None):
        bot_tokens = {
            "CE": TELEGRAM_BOT_TOKEN_CE or TELEGRAM_BOT_TOKEN_ANN,
            "RES": TELEGRAM_BOT_TOKEN_RES or TELEGRAM_BOT_TOKEN_ANN,
            "CC": TELEGRAM_BOT_TOKEN_CC or TELEGRAM_BOT_TOKEN_ANN,
            "ANN": TELEGRAM_BOT_TOKEN_ANN
        }
        chat_ids = {
            "CE": TELEGRAM_CHAT_ID_CE or TELEGRAM_CHAT_ID_ANN,
            "RES": TELEGRAM_CHAT_ID_RES or TELEGRAM_CHAT_ID_ANN,
            "CC": TELEGRAM_CHAT_ID_CC or TELEGRAM_CHAT_ID_ANN,
            "ANN": TELEGRAM_CHAT_ID_ANN
        }

        token = bot_tokens.get(route_group)
        chat_id = chat_ids.get(route_group)
        if not token or not chat_id:
            return

        snapshot = ""
        if stock_info and stock_info.get("price") != "N/A":
            snapshot = f"• <b>Price:</b> ₹{stock_info['price']} | <b>Mcap:</b> ₹{stock_info['mcap']} Cr | <b>P/E:</b> {stock_info['pe']}\n\n"

        tag_labels = {"CE": "🏭 Capacity Expansion", "RES": "📊 Financial Results", "CC": "🎙️ Concall / Presentation", "ANN": "⚡ Corporate Update"}
        label = tag_labels.get(route_group, "⚡ Alert")

        text = (
            f"<b>{label}!</b>\n\n"
            f"📌 <b>Company:</b> {html.escape(company)} (<code>{ticker}</code>)\n\n"
            f"{f'📊 <b>Stock Snapshot:</b>\n' + snapshot if snapshot else ''}"
            f"📝 <b>Headline:</b> {html.escape(headline)}\n\n"
            f"📄 <b>PDF Document:</b> {pdf_url}\n\n"
            f"📲 Follow: @financewith100rabh"
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Telegram alert error: {e}")

    def write_to_sheet(self, tab_name: str, company: str, headline: str, pdf_url: str, ticker: str):
        try:
            ws = self.sh.worksheet(tab_name)
        except WorksheetNotFound:
            ws = self.sh.add_worksheet(title=tab_name, rows="1000", cols="15")
            ws.append_row(["Date", "Company", "Headline", "PDF Link", "Status", "Ticker", "Price", "Market Cap", "PE", "High 52", "Low 52"])

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        price_formula = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "price"), "N/A")'
        mcap_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "marketcap")/10000000, "N/A")'
        pe_formula    = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "pe"), "N/A")'
        high_formula  = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "high52"), "N/A")'
        low_formula   = '=IFERROR(GOOGLEFINANCE(INDIRECT("F" & ROW()), "low52"), "N/A")'

        ws.insert_row([
            current_time, company, headline, pdf_url, "READY",
            ticker, price_formula, mcap_formula, pe_formula, high_formula, low_formula
        ], index=2, value_input_option="USER_ENTERED")

    def run(self):
        watchlist = self.get_watchlist()
        logging.info(f"Loaded {len(watchlist)} active watchlist stocks.")

        # 1. Process Screener Concalls
        screener_items = self.fetch_screener_concalls()
        for item in screener_items:
            if item['unique_key'] in self.seen_ids:
                continue
            self.write_to_sheet(item['target_tab'], item['company'], item['headline'], item['pdf_url'], item['ticker'])
            self.send_telegram_alert(item['company'], item['ticker'], item['headline'], item['pdf_url'], item['route_group'])
            self.seen_ids.add(item['unique_key'])
            self.save_seen_ids()

        # 2. Process BSE API Announcements per Watchlist Stock
        for scrip_cd, info in watchlist.items():
            announcements = self.fetch_bse_announcements(scrip_cd)
            for ann in announcements:
                headline = str(ann.get('HEADLINE', '')).strip()
                news_sub = str(ann.get('NEWSSUB', ann.get('CATEGORYNAME', ''))).strip()
                combined = f"{news_sub} {headline}"

                target_tab, route_group = self.classify_announcement(combined)
                if not target_tab:
                    continue

                attachment = ann.get('ATTACHMENTNAME')
                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
                unique_key = pdf_url if pdf_url else f"{scrip_cd}_{headline}"

                if unique_key in self.seen_ids:
                    continue

                self.write_to_sheet(target_tab, info['company'], headline, pdf_url, info['ticker_formatted'])
                self.send_telegram_alert(info['company'], info['ticker_formatted'], headline, pdf_url, route_group, info)

                self.seen_ids.add(unique_key)
                self.save_seen_ids()
                time.sleep(1)

if __name__ == "__main__":
    engine = MasterAutomationEngine()
    engine.run()
# ==============================================================================
# ------------------------- CONFIGURATION HEADER -------------------------------
# ==============================================================================

load_dotenv()

TELEGRAM_BOT_TOKEN_ANN = os.getenv("TELEGRAM_BOT_TOKEN_ANN")
TELEGRAM_CHAT_ID_ANN = os.getenv("TELEGRAM_CHAT_ID_ANN")

TELEGRAM_BOT_TOKEN_RES = os.getenv("TELEGRAM_BOT_TOKEN_RES")
TELEGRAM_CHAT_ID_RES = os.getenv("TELEGRAM_CHAT_ID_RES")

TELEGRAM_BOT_TOKEN_CE = os.getenv("TELEGRAM_BOT_TOKEN_CE")
TELEGRAM_CHAT_ID_CE = os.getenv("TELEGRAM_CHAT_ID_CE")

TELEGRAM_BOT_TOKEN_CC = os.getenv("TELEGRAM_BOT_TOKEN_CC")
TELEGRAM_CHAT_ID_CC = os.getenv("TELEGRAM_CHAT_ID_CC")

# Optional: Add a Residential/Data Proxy URL in GitHub Secrets (e.g., http://user:pass@proxy.example.com:8080)
BSE_PROXY_URL = os.getenv("BSE_PROXY_URL") or os.getenv("HTTPS_PROXY")

CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_NAME = "StockPulse Tracker"
STATE_FILE = "last_seen_master.json"

# TARGETED REGULATION 30 SUB-CATEGORIES
EXACT_TARGET_TAGS = [
    "award_of_order_receipt_of_order",
    "award of order",
    "receipt of order",
    "incorporation of subsidiary",
    "press release / media release",
    "announcement under reg 30_new aoa moa",
    "financial results",
    "financial result",
    "board meeting outcome - financial results",
    "analyst / investor meet - outcome",
    "investor presentation",
    "earnings call transcript",
    "bonus / stock split / rights issue",
    "dividend updates",
    "credit rating",
    "change in management",
    "change in directorate",
    "resignation",
    "buyback",
    "fund raising",
    "issue of securities",
    "scheme of arrangement",
    "demerger"
]

# NOISE & SAST EXCLUSION FILTERS
NOISE_KEYWORDS = [
    "trading window", "share certificate", "loss of share",
    "duplicate share", "compliance certificate", "newspaper publication",
    "clarification", "voting results", "scrutinizer report", "loss of certificate",
    "substantial acquisition", "takeovers", "regulation 29", "regulation 10", 
    "reg 29", "reg 10", "reg 29(2)", "reg 10(6)", "sast"
]

def is_noise(combined_text=""):
    text = combined_text.lower().strip()
    return any(k in text for k in NOISE_KEYWORDS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==============================================================================
# ------------------------- UNIFIED ENGINE LOGIC -------------------------------
# ==============================================================================

class MasterAutomationEngine:
    def __init__(self):
        self.gc = gspread.service_account(filename=CREDENTIALS_FILE)
        self.sh = self._connect_sheets_with_retry()
        self.watchlist_sheet = self.sh.worksheet("Watchlist")
        
        try:
            self.settings_sheet = self.sh.worksheet("Settings")
        except Exception:
            logging.info("Settings tab not found. Defaulting to WATCHLIST mode.")
            self.settings_sheet = None

        self._worksheet_cache = {}
        self.session = self._init_bse_session()

    def _init_bse_session(self) -> requests.Session:
        session = requests.Session()
        
        # Attach proxy if configured
        if BSE_PROXY_URL:
            logging.info("Routing BSE requests through configured proxy.")
            session.proxies = {
                "http": BSE_PROXY_URL,
                "https": BSE_PROXY_URL
            }

        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://www.bseindia.com/',
            'Origin': 'https://www.bseindia.com',
            'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site'
        })

        # Corrected Pre-warm: Target api.bseindia.com directly
        try:
            warm_url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubmissionData/w?pageNo=1&strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C"
            res = session.get(warm_url, timeout=10)
            if res.text.strip().startswith("<"):
                logging.warning("WAF Challenge Detected during session pre-warm on api.bseindia.com (Datacenter IP likely blocked).")
        except Exception as e:
            logging.warning(f"Session pre-warming warning: {e}")
            
        return session

    def _connect_sheets_with_retry(self, max_retries=5):
        for attempt in range(1, max_retries + 1):
            try:
                sheet = self.gc.open(GOOGLE_SHEET_NAME)
                logging.info(f"Connected to Google Sheet: '{GOOGLE_SHEET_NAME}'")
                return sheet
            except APIError as e:
                if attempt == max_retries:
                    raise e
                wait_time = attempt * 5
                logging.warning(f"Google API Error (503). Retrying in {wait_time}s... ({attempt}/{max_retries})")
                time.sleep(wait_time)

    def load_last_run_time(self) -> datetime:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    if "last_run_iso" in data:
                        return datetime.fromisoformat(data["last_run_iso"])
            except Exception as e:
                logging.warning(f"Could not read state file: {e}")
        return datetime.now() - timedelta(days=2)

    def save_last_run_time(self, run_time: datetime):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"last_run_iso": run_time.isoformat()}, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to write state file: {e}")

    def get_or_create_worksheet(self, tab_name: str):
        if tab_name in self._worksheet_cache:
            return self._worksheet_cache[tab_name]

        try:
            ws = self.sh.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            logging.info(f"Tab '{tab_name}' not found. Creating it...")
            ws = self.sh.add_worksheet(title=tab_name, rows="1000", cols="10")
            ws.append_row(["Date", "Scrip Code", "Category", "Headline", "Details", "PDF Link"])

        self._worksheet_cache[tab_name] = ws
        return ws

    def get_scan_mode(self) -> str:
        if not self.settings_sheet:
            return "WATCHLIST"
        try:
            val = str(self.settings_sheet.acell("B1").value).strip().upper()
            return "ALL_STOCKS" if val == "ALL_STOCKS" else "WATCHLIST"
        except Exception as e:
            logging.error(f"Error reading Scan Mode: {e}")
            return "WATCHLIST"

    def get_watchlist(self) -> dict:
        try:
            data = self.watchlist_sheet.get_all_records()
            watchlist = {}
            for r in data:
                is_active = str(r.get('Active', '')).strip().lower() == 'yes'
                ticker = str(r.get('Ticker', '')).strip()

                if is_active and ticker.isdigit():
                    stock_name = str(r.get('Stock Name', r.get('Company Name', 'Unknown'))).strip()
                    watchlist[ticker] = {
                        'name': stock_name,
                        'price': str(r.get('Current Price', 'N/A')).strip(),
                        'mcap': str(r.get('Market Cap (Cr)', 'N/A')).strip(),
                        'pe': str(r.get('P/E Ratio', 'N/A')).strip()
                    }
            return watchlist
        except Exception as e:
            logging.error(f"Error reading watchlist: {e}")
            return {}

    def get_processed_headlines(self) -> set:
        processed = set()
        tabs_to_check = ["Log", "Results", "Expansion", "Concall"]
        
        for tab_name in tabs_to_check:
            try:
                ws = self.sh.worksheet(tab_name)
                rows = ws.get_all_values()
                if len(rows) > 1:
                    header = [h.strip() for h in rows[0]]
                    headline_idx = header.index("Headline") if "Headline" in header else 3
                    for r in rows[1:]:
                        if len(r) > headline_idx:
                            h = str(r[headline_idx]).strip()
                            if h:
                                processed.add(h)
            except gspread.exceptions.WorksheetNotFound:
                continue
            except Exception as e:
                logging.error(f"Error reading logs from '{tab_name}': {e}")
                
        return processed

    def fetch_bse_announcements(self, scrip_cd: str = "") -> list:
        today_str = datetime.now().strftime("%Y%m%d")
        prev_str = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")

        if scrip_cd:
            url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate={prev_str}&strScrip={scrip_cd}&strSearch=P&strToDate={today_str}&strType=C"
        else:
            url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubmissionData/w?pageNo=1&strCat=-1&strPrevDate={prev_str}&strScrip=&strSearch=P&strToDate={today_str}&strType=C"

        try:
            res = self.session.get(url, timeout=15)
            res.raise_for_status()

            # Fail fast if IP is WAF-blocked (Returns HTML Challenge)
            if res.text.strip().startswith("<"):
                logging.error(f"BSE WAF IP Block Detected (Received HTML from {url}). Exiting fetch without retrying.")
                return []

            data = res.json()
            if isinstance(data, dict):
                return data.get("Table", []) or data.get("Table1", [])
            elif isinstance(data, list):
                return data
            return []
        except requests.exceptions.JSONDecodeError:
            logging.error(f"Failed to parse JSON response from BSE (scrip='{scrip_cd}'). Server blocked request with non-JSON response.")
            return []
        except Exception as e:
            logging.error(f"Failed to fetch BSE data (scrip='{scrip_cd}'): {e}")
            return []

    def classify_announcement_details(self, combined_text: str) -> tuple[str, str, str]:
        text = combined_text.lower()
        
        if any(k in text for k in ["award_of_order_receipt_of_order", "award of order", "receipt of order"]):
            return "Award_of_Order_Receipt_of_Order", "Expansion", "CE"
        if "incorporation of subsidiary" in text:
            return "Incorporation of Subsidiary", "Expansion", "CE"
        if "bonus / stock split / rights issue" in text:
            return "Bonus / Stock Split / Rights Issue", "Expansion", "CE"
        if "buyback" in text:
            return "Buyback", "Expansion", "CE"
        if "fund raising" in text or "issue of securities" in text:
            return "Fund Raising / Securities", "Expansion", "CE"

        if any(k in text for k in ["financial result", "financial results"]):
            return "Financial Results", "Results", "RES"

        if any(k in text for k in ["analyst / investor meet - outcome", "earnings call transcript", "investor presentation"]):
            return "Concall / Investor Meet", "Concall", "CC"

        if "dividend updates" in text:
            return "Dividend Update", "Log", "ANN"
        if "credit rating" in text:
            return "Credit Rating", "Log", "ANN"
        if "change in management" in text or "change in directorate" in text:
            return "Change in Management", "Log", "ANN"
        if "press release / media release" in text:
            return "Press Release", "Log", "ANN"

        return "General Announcement", "Log", "ANN"

    def get_channel_credentials(self, route_group: str):
        if route_group == "RES":
            token = TELEGRAM_BOT_TOKEN_RES or TELEGRAM_BOT_TOKEN_ANN
            chat_id = TELEGRAM_CHAT_ID_RES or TELEGRAM_CHAT_ID_ANN
        elif route_group == "CE":
            token = TELEGRAM_BOT_TOKEN_CE or TELEGRAM_BOT_TOKEN_ANN
            chat_id = TELEGRAM_CHAT_ID_CE or TELEGRAM_CHAT_ID_ANN
        elif route_group == "CC":
            token = TELEGRAM_BOT_TOKEN_CC or TELEGRAM_BOT_TOKEN_ANN
            chat_id = TELEGRAM_CHAT_ID_CC or TELEGRAM_CHAT_ID_ANN
        else:
            token = TELEGRAM_BOT_TOKEN_ANN
            chat_id = TELEGRAM_CHAT_ID_ANN
            
        return token, chat_id

   def send_telegram_alert(self, company: str, ticker: str, headline: str, pdf_url: str, route_group: str, stock_info: dict = None):
        bot_tokens = {
            "CE": TELEGRAM_BOT_TOKEN_CE or TELEGRAM_BOT_TOKEN_ANN,
            "RES": TELEGRAM_BOT_TOKEN_RES or TELEGRAM_BOT_TOKEN_ANN,
            "CC": TELEGRAM_BOT_TOKEN_CC or TELEGRAM_BOT_TOKEN_ANN,
            "ANN": TELEGRAM_BOT_TOKEN_ANN
        }
        chat_ids = {
            "CE": TELEGRAM_CHAT_ID_CE or TELEGRAM_CHAT_ID_ANN,
            "RES": TELEGRAM_CHAT_ID_RES or TELEGRAM_CHAT_ID_ANN,
            "CC": TELEGRAM_CHAT_ID_CC or TELEGRAM_CHAT_ID_ANN,
            "ANN": TELEGRAM_CHAT_ID_ANN
        }

        token = bot_tokens.get(route_group)
        chat_id = chat_ids.get(route_group)
        if not token or not chat_id:
            return

        snapshot_block = ""
        if stock_info and stock_info.get("price") != "N/A":
            snapshot_block = (
                f"📊 <b>Stock Snapshot:</b>\n"
                f"• <b>Price:</b> ₹{stock_info['price']} | <b>Mcap:</b> ₹{stock_info['mcap']} Cr | <b>P/E:</b> {stock_info['pe']}\n\n"
            )

        tag_labels = {
            "CE": "🏭 Capacity Expansion", 
            "RES": "📊 Financial Results", 
            "CC": "🎙️ Concall / Presentation", 
            "ANN": "⚡ Corporate Update"
        }
        label = tag_labels.get(route_group, "⚡ Alert")

        text = (
            f"<b>{label}!</b>\n\n"
            f"📌 <b>Company:</b> {html.escape(company)} (<code>{ticker}</code>)\n\n"
            f"{snapshot_block}"
            f"📝 <b>Headline:</b> {html.escape(headline)}\n\n"
            f"📄 <b>PDF Document:</b> {pdf_url}\n\n"
            f"📲 Follow: @financewith100rabh"
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Telegram alert error: {e}")
            
    def run(self):
        run_start_time = datetime.now()
        mode = self.get_scan_mode()
        processed_headlines = self.get_processed_headlines()
        
        cutoff_time = self.load_last_run_time()

        logging.info(f"=== Running Automation Engine in [{mode}] Mode ===")
        logging.info(f"Filtering filings published AFTER: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')}")

        announcements_to_process = []

        if mode == "WATCHLIST":
            watchlist = self.get_watchlist()
            logging.info(f"Loaded {len(watchlist)} active stocks from Watchlist.")
            for scrip_cd, stock_info in watchlist.items():
                items = self.fetch_bse_announcements(scrip_cd)
                for item in items:
                    announcements_to_process.append((scrip_cd, stock_info.get('name', 'Unknown'), stock_info, item))
        else:
            logging.info("Fetching market-wide live feed across ALL listed stocks...")
            items = self.fetch_bse_announcements(scrip_cd="")
            for item in items:
                scrip_cd = str(item.get('SCRIP_CD', '')).strip()
                company_name = str(item.get('SLONGNAME', item.get('sname', 'Unknown'))).strip()
                announcements_to_process.append((scrip_cd, company_name, None, item))

        logging.info(f"Total raw BSE announcements fetched: {len(announcements_to_process)}")

        new_count = 0
        for scrip_cd, company_name, stock_info, ann in announcements_to_process:
            headline = str(ann.get('HEADLINE', '')).strip()
            news_subject = str(ann.get('NEWS_SUBJECT', '')).strip()
            bse_category = str(ann.get('CATEGORYNAME', '')).strip()
            sub_category = str(ann.get('NEWSSUB', ann.get('SUBCATNAME', ''))).strip()
            more_desc = str(ann.get('MORE', '')).strip()

            combined_text = f"{news_subject} {bse_category} {sub_category} {headline} {more_desc}".lower()

            if not headline or headline in processed_headlines:
                continue

            news_dt_str = str(ann.get('NEWS_DT', ''))
            try:
                clean_date_str = news_dt_str.split('.')[0]
                news_date = datetime.fromisoformat(clean_date_str)
                if news_date <= cutoff_time:
                    continue
            except Exception:
                pass

            if is_noise(combined_text):
                continue

            if not any(tag in combined_text for tag in EXACT_TARGET_TAGS):
                continue

            display_cat, target_tab, route_group = self.classify_announcement_details(combined_text)

            attachment = ann.get('ATTACHMENTNAME')
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
            details = "⏳ PENDING" if pdf_url else "No PDF available."

            logging.info(f"[{mode}] Alerting ({display_cat} -> Tab: '{target_tab}'): {company_name} - {headline}")

            try:
                target_sheet = self.get_or_create_worksheet(target_tab)
                target_sheet.append_row([news_dt_str, scrip_cd, display_cat, headline, details, pdf_url])
                
                processed_headlines.add(headline)
                self.send_telegram_alert(scrip_cd, company_name, display_cat, route_group, headline, pdf_url, stock_info)
                new_count += 1
                time.sleep(1.5)
            except Exception as e:
                logging.error(f"Failed to log/alert: {e}")

        # Update checkpoint time only if fetch succeeded without block
        if announcements_to_process or new_count > 0:
            self.save_last_run_time(run_start_time)
            logging.info(f"Finished run. Processed {new_count} new announcements. Checkpoint set to {run_start_time.strftime('%Y-%m-%d %H:%M:%S')}.")
        else:
            logging.warning("No data retrieved (likely WAF block). Skipping checkpoint update to avoid missing records.")

if __name__ == "__main__":
    engine = MasterAutomationEngine()
    engine.run()
