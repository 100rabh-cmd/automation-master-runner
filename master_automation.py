from __future__ import annotations
import os
import time
import json
import logging
import warnings
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Referer': 'https://www.bseindia.com/corporates/ann.html',
            'Origin': 'https://www.bseindia.com',
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

        for tab_name in ["Expansion", "Results", "Concalls", "Log"]:
            try:
                ws = self.sh.worksheet(tab_name)
                rows = ws.get_all_values()
                for r in rows[1:200]:
                    if len(r) >= 4 and r[3].strip().startswith("http"):
                        seen.add(r[3].strip())
                    elif len(r) >= 3:
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

    def fetch_all_bse_announcements(self) -> list:
        """Har stock ke liye market-wide announcements fetch karta hai."""
        now = datetime.now()
        three_days_ago = now - timedelta(days=3)
        
        dt_ymd_today = now.strftime("%Y%m%d")
        dt_ymd_prev = three_days_ago.strftime("%Y%m%d")
        dt_slash_today = now.strftime("%d/%m/%Y")
        dt_slash_prev = three_days_ago.strftime("%d/%m/%Y")

        candidates = [
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate={dt_ymd_prev}&strScrip=&strSearch=D&strToDate={dt_ymd_today}&strType=C",
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate={dt_slash_prev}&strScrip=&strSearch=D&strToDate={dt_slash_today}&strType=C",
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnouncementsList/w?pageno=1&strCat=-1&strPrevDate={dt_ymd_prev}&strScrip=&strSearch=D&strToDate={dt_ymd_today}&strType=C"
        ]

        for idx, url in enumerate(candidates, 1):
            try:
                res = self.session.get(url, timeout=12)
                logging.info(f"Checking BSE Endpoint #{idx} | Status: {res.status_code}")
                if res.status_code == 200:
                    data = res.json()
                    items = []
                    if isinstance(data, dict):
                        items = data.get("Table") or data.get("Table1") or data.get("Table2") or []
                    elif isinstance(data, list):
                        items = data

                    if items:
                        logging.info(f"Successfully fetched {len(items)} live announcements across all stocks.")
                        return items
            except Exception as e:
                logging.error(f"Error querying BSE candidate #{idx}: {e}")
                continue

        logging.warning("All market-wide BSE endpoints returned 0 results.")
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

    def classify_announcement(self, text: str) -> tuple[str | None, str | None]:
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

    def send_telegram_alert(self, company: str, ticker: str, headline: str, pdf_url: str, route_group: str):
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
        logging.info("Starting global market scanning engine...")

        # 1. Process Screener Concalls
        screener_items = self.fetch_screener_concalls()
        for item in screener_items:
            if item['unique_key'] in self.seen_ids:
                continue
            self.write_to_sheet(item['target_tab'], item['company'], item['headline'], item['pdf_url'], item['ticker'])
            self.send_telegram_alert(item['company'], item['ticker'], item['headline'], item['pdf_url'], item['route_group'])
            self.seen_ids.add(item['unique_key'])
            self.save_seen_ids()

        # 2. Process ALL BSE Corporate Announcements
        announcements = self.fetch_all_bse_announcements()
        for ann in announcements:
            scrip_cd = str(ann.get('SCRIP_CD', '')).strip()
            company_name = str(ann.get('SLONGNAME', ann.get('COMPANY_NAME', 'Unknown'))).strip()
            ticker_fmt = f"BOM:{scrip_cd}" if scrip_cd else ""

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

            self.write_to_sheet(target_tab, company_name, headline, pdf_url, ticker_fmt)
            self.send_telegram_alert(company_name, ticker_fmt, headline, pdf_url, route_group)

            self.seen_ids.add(unique_key)
            self.save_seen_ids()

if __name__ == "__main__":
    engine = MasterAutomationEngine()
    engine.run()
