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

# Categories and keywords to ignore completely
NOISE_KEYWORDS = [
    "general",
    "trading window",
    "share certificate",
    "loss of share",
    "duplicate share",
    "compliance certificate",
    "newspaper publication",
    "clarification",
    "voting results",
    "scrutinizer report"
]

def is_noise(category="", title=""):
    """Returns True if the announcement matches any generic noise keyword."""
    combined_text = f"{category} {title}".lower().strip()
    return any(keyword in combined_text for keyword in NOISE_KEYWORDS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==============================================================================
# ------------------------- CORE AUTOMATION LOGIC ------------------------------
# ==============================================================================

class StockScanner:
    def __init__(self):
        self.gc = gspread.service_account(filename=CREDENTIALS_FILE)
        self.sh = self.gc.open(GOOGLE_SHEET_NAME)
        self.watchlist_sheet = self.sh.worksheet("Watchlist")
        self.logs_sheet = self.sh.worksheet("Log")

    def get_watchlist(self) -> dict:
        """
        Returns a dictionary of active stocks with scrip_code as key:
        {
            '533519': {
                'name': 'LTF',
                'price': '307',
                'mcap': '79793',
                'pe': 'N/A'
            }, ...
        }
        """
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
        try:
            records = self.logs_sheet.get_all_records()
            return {str(r.get('Headline', '')).strip() for r in records if r.get('Headline')}
        except Exception as e:
            logging.error(f"Could not read previous logs: {e}")
            return set()

    def fetch_scrip_announcements(self, scrip_cd: str) -> list:
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
            logging.error(f"Failed to fetch data for scrip {scrip_cd}: {e}")
            return []

    def classify_news_strict(self, headline: str) -> str:
        """Purely keyword-based classification."""
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

    def send_telegram_alert(self, scrip_cd: str, stock_info: dict, category: str, headline: str, pdf_url: str):
        stock_name = stock_info.get('name', 'Unknown')
        price = stock_info.get('price', 'N/A')
        mcap = stock_info.get('mcap', 'N/A')
        pe = stock_info.get('pe', 'N/A')

        metrics_block = ""
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
        watchlist = self.get_watchlist()
        processed_headlines = self.get_processed_headlines()
        cutoff_time = datetime.now() - timedelta(days=2)

        logging.info(f"Loaded {len(watchlist)} active stocks from Watchlist.")
        logging.info(f"Checking updates. Ignoring news older than {cutoff_time.strftime('%Y-%m-%d')}")

        for scrip_cd, stock_info in watchlist.items():
            announcements = self.fetch_scrip_announcements(scrip_cd)

            for ann in announcements:
                headline = str(ann.get('HEADLINE', '')).strip()
                bse_category = str(ann.get('CATEGORYNAME', '')).strip()

                # 1. Check if already processed
                if not headline or headline in processed_headlines:
                    continue

                # 2. Time Filter (48-hour window)
                news_dt_str = str(ann.get('NEWS_DT', ''))
                try:
                    clean_date_str = news_dt_str.split('.')[0]
                    news_date = datetime.fromisoformat(clean_date_str)
                    if news_date < cutoff_time:
                        continue
                except Exception:
                    logging.warning(f"Could not parse date '{news_dt_str}' for {headline}. Skipping time check.")

                # 3. High impact filter
                high_impact_keywords = ['order', 'result', 'presentation', 'earnings', 'management', 'insider', 'award', 'contract', 'bagging']
                if not any(word in headline.lower() for word in high_impact_keywords):
                    continue

                # 4. Skip generic noise & general category announcements
                if is_noise(bse_category, headline):
                    continue

                category = self.classify_news_strict(headline)
                if category == "General":
                    continue

                attachment = ann.get('ATTACHMENTNAME')
                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}" if attachment else ""
                details = "⏳ PENDING" if pdf_url else "No PDF available."

                logging.info(f"Logging new announcement: {stock_info.get('name')} - {headline}")

                try:
                    # Log to Google Sheets
                    self.logs_sheet.append_row([news_dt_str, scrip_cd, category, headline, details, pdf_url])
                    processed_headlines.add(headline)

                    # Send Telegram Alert with stock metrics
                    self.send_telegram_alert(scrip_cd, stock_info, category, headline, pdf_url)

                    time.sleep(2)

                except Exception as e:
                    logging.error(f"Failed to log/alert: {e}")

if __name__ == "__main__":
    bot = StockScanner()
    bot.run()
