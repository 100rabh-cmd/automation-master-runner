import os
import time
import logging
import warnings
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import gspread

warnings.filterwarnings("ignore")

# ==============================================================================
# ------------------------- CONFIGURATION HEADER -------------------------------
# ==============================================================================

load_dotenv()

TELEGRAM_BOT_TOKEN_ANN = os.getenv("TELEGRAM_BOT_TOKEN_ANN")
TELEGRAM_CHAT_ID_ANN = os.getenv("TELEGRAM_CHAT_ID_ANN")
CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_NAME = "StockPulse Tracker"

# Noise keywords to ignore across both modes
NOISE_KEYWORDS = [
    "general", "trading window", "share certificate", "loss of share",
    "duplicate share", "compliance certificate", "newspaper publication",
    "clarification", "voting results", "scrutinizer report"
]

HIGH_IMPACT_KEYWORDS = [
    'order', 'result', 'presentation', 'earnings', 'management', 
    'insider', 'award', 'contract', 'bagging', 'concall', 'expansion'
]

def is_noise(category="", title=""):
    """Returns True if announcement contains noise keywords."""
    combined = f"{category} {title}".lower().strip()
    return any(k in combined for k in NOISE_KEYWORDS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==============================================================================
# ------------------------- UNIFIED ENGINE LOGIC -------------------------------
# ==============================================================================

class MasterAutomationEngine:
    def __init__(self):
        self.gc = gspread.service_account(filename=CREDENTIALS_FILE)
        self.sh = self.gc.open(GOOGLE_SHEET_NAME)
        self.watchlist_sheet = self.sh.worksheet("Watchlist")
        self.logs_sheet = self.sh.worksheet("Log")
        
        # Load or create Settings tab
        try:
            self.settings_sheet = self.sh.worksheet("Settings")
        except Exception:
            logging.info("Settings tab not found. Defaulting to WATCHLIST mode.")
            self.settings_sheet = None

    def get_scan_mode(self) -> str:
        """Reads Cell B1 from Settings sheet. Returns 'WATCHLIST' or 'ALL_STOCKS'."""
        if not self.settings_sheet:
            return "WATCHLIST"
        try:
            val = str(self.settings_sheet.acell("B1").value).strip().upper()
            return "ALL_STOCKS" if val == "ALL_STOCKS" else "WATCHLIST"
        except Exception as e:
            logging.error(f"Error reading Scan Mode setting: {e}")
            return "WATCHLIST"

    def get_watchlist(self) -> dict:
        """Fetches active stocks mapping scrip code -> metadata."""
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
        """Reads previously logged headlines from Log tab."""
        try:
            records = self.logs_sheet.get_all_records()
            return {str(r.get('Headline', '')).strip() for r in records if r.get('Headline')}
        except Exception as e:
            logging.error(f"Could not read previous logs: {e}")
            return set()

    def fetch_bse_announcements(self, scrip_cd: str = "") -> list:
        """Fetches announcements. If scrip_cd is empty, fetches market-wide feed."""
        url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip={scrip_cd}&strSearch=P&strToDate=&strType=C"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.bseindia.com/',
            'Accept': 'application/json, text/plain, */*'
        }
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            return data.get("Table", [])
        except Exception as e:
            logging.error(f"Failed to fetch BSE data (scrip='{scrip_cd}'): {e}")
            return []

    def classify_news_strict(self, headline: str) -> str:
        h = headline.lower()
        if any(k in h for k in ["award", "order", "contract", "bagging"]):
            return "Expansion / Order"
        if any(k in h for k in ["presentation", "earnings call", "concall", "transcript"]):
            return "Investor Presentation"
        if any(k in h for k in ["management", "resignation", "appointment", "director"]):
            return "Management Change"
        if any(k in h for k in ["financial results", "outcome of board meeting"]):
            return "Financial Results"
        if "insider" in h or "trading window" in h:
            return "Insider Trading"
        return "General"

    def send_telegram_alert(self, scrip_cd: str, stock_name: str, category: str, headline: str, pdf_url: str, stock_info: dict = None):
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
            f"<b>Company:</b> {stock_name} (<code>{scrip_cd}</code>)\n"
            f"<b>Category:</b> {category}\n\n"
            f"{metrics_block}"
            f"<b>Headline:</b> {headline}\n"
        )
        if pdf_url:
            text += f"\n📄 <b>PDF Document:</b> {pdf_url}"

        text += "\n\n📲 Follow: @financewith100rabh"

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_ANN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID_ANN,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"Failed to send Telegram alert: {e}")

    def run(self):
        mode = self.get_scan_mode()
        processed_headlines = self.get_processed_headlines()
        cutoff_time = datetime.now() - timedelta(days=2)

        logging.info(f"=== Running Automation Engine in [{mode}] Mode ===")

        announcements_to_process = []

        if mode == "WATCHLIST":
            watchlist = self.get_watchlist()
            logging.info(f"Loaded {len(watchlist)} active stocks from Watchlist.")
            for scrip_cd, stock_info in watchlist.items():
                items = self.fetch_bse_announcements(scrip_cd)
                for item in items:
                    announcements_to_process.append((scrip_cd, stock_info.get('name', 'Unknown'), stock_info, item))
        else:
            # ALL_STOCKS Mode: Fetch single market-wide feed
            logging.info("Fetching market-wide live feed across ALL listed stocks...")
            items = self.fetch_bse_announcements(scrip_cd="")
            for item in items:
                scrip_cd = str(item.get('SCRIP_CD', '')).strip()
                company_name = str(item.get('SLONGNAME', item.get('sname', 'Unknown'))).strip()
                announcements_to_process.append((scrip_cd, company_name, None, item))

        # Process gathered announcements
        for scrip_cd, company_name, stock_info, ann in announcements_to_process:
            headline = str(ann.get('HEADLINE', '')).strip()
            bse_category = str(ann.get('CATEGORYNAME', '')).strip()

            # 1. Deduplication
            if not headline or headline in processed_headlines:
                continue

            # 2. Time Filter (48-hour cutoff)
            news_dt_str = str(ann.get('NEWS_DT', ''))
            try:
                clean_date_str = news_dt_str.split('.')[0]
                news_date = datetime.fromisoformat(clean_date_str)
                if news_date < cutoff_time:
                    continue
            except Exception:
                pass

            # 3. High Impact Keyword Check
            if not any(word in headline.lower() for word in HIGH_IMPACT_KEYWORDS):
                continue

            # 4. Noise Filter Check
            if is_noise(bse_category, headline):
                continue

            category = self.classify_news_strict(headline)
            if category == "General":
                continue

            attachment = ann.get('ATTACHMENTNAME')
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
            details = "⏳ PENDING" if pdf_url else "No PDF available."

            logging.info(f"[{mode}] Alerting: {company_name} - {headline}")

            try:
                self.logs_sheet.append_row([news_dt_str, scrip_cd, category, headline, details, pdf_url])
                processed_headlines.add(headline)
                self.send_telegram_alert(scrip_cd, company_name, category, headline, pdf_url, stock_info)
                time.sleep(1.5)
            except Exception as e:
                logging.error(f"Failed to log/alert: {e}")

if __name__ == "__main__":
    engine = MasterAutomationEngine()
    engine.run()
