import os
import time
import html
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
LOG_TAB = "Log"
WATCHLIST_TAB = "Watchlist"

# 1. EXCLUSION FILTERS (Skip Expansion, Concalls/Presentations, Results & Noise)
EXCLUDE_KEYWORDS = [
    # Expansion & Commissioning (Handled by Expansion Script)
    "expansion", "capacity", "commercial production", "commissioning",
    "new plant", "new facility", "capacity addition", "greenfield", "brownfield",
    
    # Concalls & Presentations (Handled by Concall Script)
    "concall", "earnings call", "transcript", "investor presentation", 
    "analyst presentation", "audio recording",
    
    # Financial Results (Handled by Results Script)
    "financial result", "audited result", "unaudited result", 
    "outcome of board meeting", "quarterly result", "financial statement",
    
    # Compliance & Routine Noise
    "trading window", "loss of share", "duplicate share", "compliance certificate",
    "newspaper publication", "clarification", "voting result", "scrutinizer report",
    "closure of trading window", "general", "demat", "confirmation certificate"
]

# 2. INCLUSION CATEGORIES & KEYWORDS
CATEGORY_MAPPINGS = {
    "Capital Action / Corporate Finance": [
        "bonus", "split", "rights issue", "buyback", "dividend", 
        "fund raising", "qip", "preferential issue", "issue of securities", 
        "allotment of shares", "debentures"
    ],
    "M&A / Corporate Restructuring": [
        "acquisition", "subsidiary", "incorporation", "demerger", 
        "merger", "scheme of arrangement", "joint venture", "slump sale",
        "new aoa moa", "reg 30"
    ],
    "Governance & Leadership": [
        "appointment", "resignation", "re-appointment", "change in directorate",
        "change in management", "kmp", "key managerial", "managing director", 
        "ceo", "cfo"
    ],
    "Credit Rating": [
        "credit rating", "rating agency", "crisil", "icra", "care", "ind-ra"
    ],
    "Orders & Contracts": [
        "award of order", "receipt of order", "contract", "bagging"
    ],
    "Investor / Analyst Meet": [
        "analyst meet", "investor meet", "analyst / investor meet"
    ],
    "Press & Business Update": [
        "press release", "media release", "business update"
    ]
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==============================================================================
# ------------------------- CORE AUTOMATION LOGIC ------------------------------
# ==============================================================================

class GeneralAnnouncementsScanner:
    def __init__(self):
        self.gc = gspread.service_account(filename=CREDENTIALS_FILE)
        self.sh = self.gc.open(GOOGLE_SHEET_NAME)
        self.watchlist_sheet = self.sh.worksheet(WATCHLIST_TAB)
        self.logs_sheet = self.sh.worksheet(LOG_TAB)

    def get_watchlist(self) -> dict:
        """Fetches active BSE scrip codes and company info from Watchlist."""
        try:
            data = self.watchlist_sheet.get_all_records()
            watchlist = {}
            for r in data:
                is_active = str(r.get('Active', '')).strip().lower() in ['yes', 'true', '1']
                ticker = str(r.get('Ticker', '')).replace("BOM:", "").replace("NSE:", "").strip()

                if is_active and ticker.isdigit():
                    stock_name = str(r.get('Stock Name', r.get('Company Name', 'Unknown'))).strip()
                    watchlist[ticker] = {
                        'name': stock_name,
                        'ticker_formatted': f"BOM:{ticker}",
                        'price': str(r.get('Current Price', 'N/A')).strip(),
                        'mcap': str(r.get('Market Cap (Cr)', 'N/A')).strip(),
                        'pe': str(r.get('P/E Ratio', 'N/A')).strip()
                    }
            return watchlist
        except Exception as e:
            logging.error(f"Error reading watchlist: {e}")
            return {}

    def get_processed_headlines(self) -> set:
        """Fetches already processed headlines from Log sheet to avoid duplicate alerts."""
        try:
            records = self.logs_sheet.get_all_records()
            return {str(r.get('Headline', '')).strip() for r in records if r.get('Headline')}
        except Exception as e:
            logging.error(f"Could not read previous logs: {e}")
            return set()

    def fetch_scrip_announcements(self, scrip_cd: str) -> list:
        """Fetches corporate filings directly from BSE API."""
        url = f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=&strScrip={scrip_cd}&strSearch=P&strToDate=&strType=C"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.bseindia.com/',
            'Accept': 'application/json, text/plain, */*'
        }
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            return res.json().get("Table", [])
        except Exception as e:
            logging.error(f"Failed to fetch data for scrip {scrip_cd}: {e}")
            return []

    def classify_announcement(self, category_name: str, headline: str) -> str:
        """
        Filters out Expansion, Concalls, Results & Noise.
        Returns the category string if matched, otherwise None.
        """
        combined = f"{category_name} {headline}".lower().strip()

        # 1. Exclude unwanted types
        if any(ex in combined for ex in EXCLUDE_KEYWORDS):
            return None

        # 2. Match target categories
        for cat_label, keywords in CATEGORY_MAPPINGS.items():
            if any(k in combined for k in keywords):
                return cat_label

        return None

    def send_telegram_alert(self, scrip_cd: str, stock_info: dict, category: str, headline: str, pdf_url: str):
        if not TELEGRAM_BOT_TOKEN_ANN or not TELEGRAM_CHAT_ID_ANN:
            logging.warning("Telegram skipped: Credentials missing.")
            return

        stock_name = html.escape(stock_info.get('name', 'Unknown'))
        safe_headline = html.escape(headline)
        safe_category = html.escape(category)
        
        price = stock_info.get('price', 'N/A')
        mcap = stock_info.get('mcap', 'N/A')
        pe = stock_info.get('pe', 'N/A')

        metrics_block = ""
        if price != "N/A" or mcap != "N/A":
            metrics_block = (
                f"📊 <b>Stock Snapshot:</b>\n"
                f"• <b>Price:</b> ₹{price} | <b>Mcap:</b> ₹{mcap} Cr | <b>P/E:</b> {pe}\n\n"
            )

        text = (
            f"⚡ <b>Corporate Announcement Alert</b>\n\n"
            f"📌 <b>Company:</b> {stock_name} (<code>BOM:{scrip_cd}</code>)\n"
            f"🏷️ <b>Category:</b> {safe_category}\n\n"
            f"{metrics_block}"
            f"📝 <b>Headline:</b> {safe_headline}\n"
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
        watchlist = self.get_watchlist()
        processed_headlines = self.get_processed_headlines()
        cutoff_time = datetime.now() - timedelta(days=2)

        logging.info(f"Loaded {len(watchlist)} active stocks from Watchlist.")
        logging.info(f"Scanning filings (ignoring Expansion, Concalls, Results, and news older than {cutoff_time.strftime('%Y-%m-%d')})...")

        for scrip_cd, stock_info in watchlist.items():
            announcements = self.fetch_scrip_announcements(scrip_cd)

            for ann in announcements:
                headline = str(ann.get('HEADLINE', '')).strip()
                bse_category = str(ann.get('CATEGORYNAME', '')).strip()

                if not headline or headline in processed_headlines:
                    continue

                # Parse publication date (48-hour window)
                news_dt_str = str(ann.get('NEWS_DT', ''))
                try:
                    clean_date_str = news_dt_str.split('.')[0]
                    news_date = datetime.fromisoformat(clean_date_str)
                    if news_date < cutoff_time:
                        continue
                except Exception:
                    pass

                # Classify & Filter
                category = self.classify_announcement(bse_category, headline)
                if not category:
                    continue

                attachment = ann.get('ATTACHMENTNAME')
                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
                details = "⏳ PENDING" if pdf_url else "No PDF available."

                logging.info(f"Matched [{category}]: {stock_info.get('name')} - {headline}")

                try:
                    # Append row to Log sheet
                    self.logs_sheet.append_row([news_dt_str, scrip_cd, category, headline, details, pdf_url])
                    processed_headlines.add(headline)

                    # Send Telegram Notification
                    self.send_telegram_alert(scrip_cd, stock_info, category, headline, pdf_url)

                    time.sleep(1.5)

                except Exception as e:
                    logging.error(f"Failed to process announcement: {e}")

if __name__ == "__main__":
    bot = GeneralAnnouncementsScanner()
    bot.run()
