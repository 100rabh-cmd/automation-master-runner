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

CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_NAME = "StockPulse Tracker"
# Updated state file name to match GitHub Actions file pattern (last_seen_*.json)
STATE_FILE = "last_seen_master.json"

# TARGETED REGULATION 30 SUB-CATEGORIES
EXACT_TARGET_TAGS = [
    # Orders & Expansion
    "award_of_order_receipt_of_order",
    "award of order",
    "receipt of order",
    "incorporation of subsidiary",
    "press release / media release",
    "announcement under reg 30_new aoa moa",
    
    # Financial Results
    "financial results",
    "financial result",
    "board meeting outcome - financial results",
    
    # Investor Meets & Calls
    "analyst / investor meet - outcome",
    "investor presentation",
    "earnings call transcript",
    
    # Capital & Governance Updates
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
    # SAST Disclosures to Ignore Completely
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
        # Fixed: Routes to AnnSubmissionData/w for market-wide ALL_STOCKS scan
        if scrip_cd:
            url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip={scrip_cd}&strSearch=P&strToDate=&strType=C"
        else:
            url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubmissionData/w?PageNo=1&strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://www.bseindia.com/',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://www.bseindia.com'
        }
        try:
            res = requests.get(url, headers=headers, timeout=15)
            res.raise_for_status()
            data = res.json()
            
            if isinstance(data, dict):
                return data.get("Table", []) or data.get("Table1", [])
            return []
        except Exception as e:
            logging.error(f"Failed to fetch BSE data (scrip='{scrip_cd}'): {e}")
            return []

    def classify_announcement_details(self, combined_text: str) -> tuple[str, str, str]:
        text = combined_text.lower()
        
        # 1. Orders & Expansion
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

        # 2. Financial Results
        if any(k in text for k in ["financial result", "financial results"]):
            return "Financial Results", "Results", "RES"

        # 3. Concalls & Investor Meets
        if any(k in text for k in ["analyst / investor meet - outcome", "earnings call transcript", "investor presentation"]):
            return "Concall / Investor Meet", "Concall", "CC"

        # 4. Other Reg 30 Filings
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

    def send_telegram_alert(self, scrip_cd: str, stock_name: str, display_category: str, route_group: str, headline: str, pdf_url: str, stock_info: dict = None):
        bot_token, chat_id = self.get_channel_credentials(route_group)
        
        if not bot_token or not chat_id:
            logging.warning(f"No Telegram credentials for group '{route_group}'. Skipping alert.")
            return

        metrics_block = ""
        if stock_info:
            price = stock_info.get('price', 'N/A')
            mcap = stock_info.get('mcap', 'N/A')
            pe = stock_info.get('pe', 'N/A')
            if price != "N/A" or mcap != "N/A":
                metrics_block = (
                    f"📊 <b>Stock Snapshot:</b>\n"
                    f"• Price: ₹{price} | Mcap: ₹{mcap} Cr | P/E: {pe}\n\n"
                )

        text = (
            f"⚡ <b>High-Impact Stock Alert</b>\n\n"
            f"<b>Company:</b> {stock_name} - {scrip_cd}\n"
            f"<b>Category:</b> {display_category}\n\n"
            f"{metrics_block}"
            f"<b>Headline:</b> {headline}\n"
        )
        if pdf_url:
            text += f"\n📄 <b>PDF Document:</b> {pdf_url}"

        text += "\n\n📲 Follow: @financewith100rabh"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Failed to send Telegram alert for {display_category}: {e}")

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

            # Time Window Check
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

        self.save_last_run_time(run_start_time)
        logging.info(f"Finished run. Processed {new_count} new announcements. Checkpoint set to {run_start_time.strftime('%Y-%m-%d %H:%M:%S')}.")

if __name__ == "__main__":
    engine = MasterAutomationEngine()
    engine.run()
