import logging
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from models.stock import Concept, StockConcept

logger = logging.getLogger(__name__)

THS_CONCEPT_URL = "http://q.10jqka.com.cn/gn/detail/code/{code}/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _scrape_concept_stocks(concept_code: str) -> list[dict]:
    """从同花顺概念详情页抓取成分股列表"""
    url = THS_CONCEPT_URL.format(code=concept_code)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = "gbk"
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.warning("fetch concept page failed: %s, %s", concept_code, e)
        return []

    stocks = []
    # Try the main stock table in the page
    for tr in soup.select("table.m-table tbody tr, table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        code_tag = tds[0].find("a") or tds[0]
        name_tag = tds[1].find("a") or tds[1]
        code_text = code_tag.get_text(strip=True)
        name_text = name_tag.get_text(strip=True)
        # Filter to A-share 6-digit codes
        if re.match(r"^\d{6}$", code_text):
            stocks.append({"code": code_text, "name": name_text})

    return stocks


class AkShareDownloader:

    def __init__(self, session: Session):
        self.session = session
        import akshare as ak
        self.ak = ak

    def download_concepts(self) -> tuple[int, int]:
        """下载同花顺概念列表及成分股，返回(概念数量, 关联数量)"""
        logger.info("fetching concept list from akshare ...")
        df: pd.DataFrame = self.ak.stock_board_concept_name_ths()
        if df.empty:
            logger.warning("empty concept list")
            return 0, 0

        concept_count = 0
        relation_count = 0

        # Save concepts
        for _, row in df.iterrows():
            code = row["code"]
            name = row["name"]
            existing = self.session.get(Concept, code)
            if existing:
                existing.name = name
            else:
                self.session.add(Concept(code=code, name=name))
                concept_count += 1
        self.session.commit()
        logger.info("concepts saved: %d new, %d total", concept_count, len(df))

        # Scrape constituent stocks for each concept from THS detail page
        for _, row in df.iterrows():
            code = row["code"]
            name = row["name"]

            stocks = _scrape_concept_stocks(code)
            if not stocks:
                logger.info("concept %s(%s): 0 stocks (or scrape failed)", name, code)
                continue

            for st in stocks:
                existing = self.session.query(StockConcept).filter_by(
                    stock_code=st["code"], concept_code=code,
                ).first()
                if not existing:
                    self.session.add(StockConcept(
                        stock_code=st["code"], concept_code=code,
                    ))
                    relation_count += 1

            self.session.commit()
            logger.info("concept %s(%s): %d stocks", name, code, len(stocks))

        return concept_count, relation_count
