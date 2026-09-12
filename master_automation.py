def fetch_all_bse_announcements(self) -> list:
        """Fetches live market-wide announcements with fallback endpoint strategies."""
        now = datetime.now()
        three_days_ago = now - timedelta(days=3)
        
        dt_ymd_today = now.strftime("%Y%m%d")
        dt_ymd_prev = three_days_ago.strftime("%Y%m%d")
        
        dt_slash_today = now.strftime("%d/%m/%Y")
        dt_slash_prev = three_days_ago.strftime("%d/%m/%Y")

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://www.bseindia.com',
            'Referer': 'https://www.bseindia.com/corporates/ann.html'
        })

        # Candidate URLs covering BSE's query modes and date formats
        candidates = [
            # 1. Date Range Search (strSearch=D) with YYYYMMDD
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate={dt_ymd_prev}&strScrip=&strSearch=D&strToDate={dt_ymd_today}&strType=C",
            # 2. Date Range Search with DD/MM/YYYY format
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate={dt_slash_prev}&strScrip=&strSearch=D&strToDate={dt_slash_today}&strType=C",
            # 3. Dedicated Announcements List endpoint
            f"https://api.bseindia.com/BseIndiaAPI/api/AnnouncementsList/w?pageno=1&strCat=-1&strPrevDate={dt_ymd_prev}&strScrip=&strSearch=D&strToDate={dt_ymd_today}&strType=C"
        ]

        for url in candidates:
            try:
                res = self.session.get(url, timeout=12)
                if res.status_code == 200:
                    data = res.json()
                    
                    items = []
                    if isinstance(data, dict):
                        # Resolve BSE's dynamic response keys ('Table', 'Table1', 'Table2')
                        items = data.get("Table") or data.get("Table1") or data.get("Table2") or []
                    elif isinstance(data, list):
                        items = data

                    if items:
                        logging.info(f"Successfully retrieved {len(items)} BSE filings.")
                        return items
            except Exception as e:
                logging.error(f"Error querying BSE candidate endpoint: {e}")
                continue

        logging.warning("All BSE API endpoint strategies returned 0 announcements.")
        return []
