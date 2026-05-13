import logging
import re
from datetime import date

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from config import THS_COOKIE
from logic.progress import start, step, finish
from models.stock import Concept, ConceptDaily, StockConcept

logger = logging.getLogger(__name__)

THS_PAGE_URL = (
    "http://q.10jqka.com.cn/gn/detail/"
    "order/asc/op/code/page/{page}/code/{code}/"
)
# THS limits unauthenticated access to 5 pages; no limit when logged in
MAX_PAGES = 5

_session = None


def _get_session() -> requests.Session:
    """获取带 cookie 的 requests session"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://q.10jqka.com.cn/",
        })
        if THS_COOKIE:
            for part in THS_COOKIE.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    _session.cookies.set(k.strip(), v.strip())
            logger.info("THS session with cookie (%d chars)", len(THS_COOKIE))
        else:
            logger.info("THS session (anonymous, max %d pages)", MAX_PAGES)
    return _session


def _add_exchange_prefix(code: str) -> str:
    """给6位代码加上交易所前缀 (sh/sz/bj) 以匹配 baostock 格式"""
    if not re.match(r"^\d{6}$", code):
        return code
    if code.startswith("6"):
        return f"sh.{code}"
    elif code.startswith(("0", "3")):
        return f"sz.{code}"
    elif code.startswith(("4", "8", "9")):
        return f"bj.{code}"
    return code


def _extract_page_stocks(soup: BeautifulSoup) -> list[dict]:
    """从页面 soup 提取成分股代码和名称"""
    stocks = []
    for tr in soup.select("table.m-table tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        code_text = tds[1].get_text(strip=True)
        name_text = tds[2].get_text(strip=True)
        if re.match(r"^\d{6}$", code_text):
            stocks.append({"code": _add_exchange_prefix(code_text), "name": name_text})
    return stocks


def _is_logged_in(soup: BeautifulSoup) -> bool:
    """检查页面是否需要登录"""
    scripts = soup.find_all("script")
    for s in scripts:
        if s.string and "upass.10jqka.com.cn/login" in s.string:
            return False
    return True


def _scrape_concept_all_stocks(concept_code: str) -> list[dict]:
    """抓取同花顺概念的全部成分股（遍历所有分页）"""
    session = _get_session()
    all_stocks = []

    # Fetch page 1 first to get total page count
    url = THS_PAGE_URL.format(page=1, code=concept_code)
    try:
        r = session.get(url, timeout=15)
        r.encoding = "gbk"
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.warning("fetch page 1 failed for %s: %s", concept_code, e)
        return []

    if not _is_logged_in(soup):
        logger.warning("THS login required — falling back to anonymous mode")

    # Parse total pages
    total_pages = 1
    pager = soup.find(class_="m-pager")
    if pager:
        info = pager.find(class_="page_info")
        if info:
            m = re.search(r"(\d+)/(\d+)", info.get_text())
            if m:
                real_pages = int(m.group(2))
                capped = min(real_pages, MAX_PAGES)
                if THS_COOKIE:
                    total_pages = real_pages
                else:
                    total_pages = capped
                    if real_pages > MAX_PAGES:
                        logger.info("concept %s has %d pages, capped to %d "
                                    "(login to get all)", concept_code, real_pages,
                                    MAX_PAGES)

    stocks = _extract_page_stocks(soup)
    all_stocks.extend(stocks)
    logger.debug("concept %s page 1/%d: %d stocks", concept_code, total_pages, len(stocks))

    # Fetch remaining pages
    for page in range(2, total_pages + 1):
        try:
            url = THS_PAGE_URL.format(page=page, code=concept_code)
            r = session.get(url, timeout=15)
            r.encoding = "gbk"
            soup = BeautifulSoup(r.text, "lxml")
            stocks = _extract_page_stocks(soup)
            all_stocks.extend(stocks)
            logger.debug("concept %s page %d/%d: %d stocks",
                         concept_code, page, total_pages, len(stocks))
        except Exception as e:
            logger.warning("fetch page %d failed for %s: %s", page, concept_code, e)
            continue

    return all_stocks


class AkShareDownloader:

    def __init__(self, session: Session):
        self.session = session
        import akshare as ak
        self.ak = ak

    def download_concepts(self, codes_need=None) -> tuple[int, int]:
        """下载同花顺概念列表及全部成分股，返回(概念数量, 关联数量)。
        可传入 codes_need 只下载指定概念代码"""
        need_set = set(codes_need) if codes_need else None
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

        # Scrape all constituent stocks for each concept
        total_concepts = len(df)
        start("concept", total_concepts, label="开始下载概念成分股...")
        for i, (_, row) in enumerate(df.iterrows()):
            code = row["code"]
            name = row["name"]

            # 如果指定了需要下载的列表，跳过已有数据的概念
            if need_set and code not in need_set:
                step("concept", 1, label=f"{i+1}/{total_concepts} {name} (跳过)")
                continue

            step("concept", 0, label=f"{i+1}/{total_concepts} {name}")
            stocks = _scrape_concept_all_stocks(code)
            if not stocks:
                logger.info("concept %s(%s): 0 stocks (or scrape failed)", name, code)
                step("concept", 1, label=f"{i+1}/{total_concepts} {name} (空)")
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
            step("concept", 1, label=f"{i+1}/{total_concepts} {name} ({len(stocks)}只)")
            logger.info("concept %s(%s): %d stocks total", name, code, len(stocks))

        finish("concept")
        return concept_count, relation_count

    def download_concept_daily(self, days: int = 1000, concepts=None) -> int:
        """下载概念指数日K线，返回总行数（可传入概念列表只下载指定概念）"""
        if concepts is None:
            concepts = self.session.query(Concept).all()
        if not concepts:
            logger.warning("no concepts in DB, download concepts first")
            return 0

        total = len(concepts)
        start("concept_daily", total, label="开始下载概念指数日K线...")
        row_count = 0

        for i, c in enumerate(concepts):
            try:
                df = self.ak.stock_board_concept_index_ths(
                    symbol=c.name,
                    end_date=date.today().strftime("%Y%m%d"),
                )
                if df.empty:
                    step("concept_daily", 1, label=f"{i+1}/{total} {c.name} (空)")
                    continue

                df = df.tail(days)
                for _, r in df.iterrows():
                    td = str(r["日期"])[:10]
                    existing = self.session.query(ConceptDaily).filter_by(
                        concept_code=c.code, trade_date=td,
                    ).first()
                    vals = {
                        "open": float(r["开盘价"]) if r.get("开盘价") is not None else None,
                        "high": float(r["最高价"]) if r.get("最高价") is not None else None,
                        "low": float(r["最低价"]) if r.get("最低价") is not None else None,
                        "close": float(r["收盘价"]) if r.get("收盘价") is not None else None,
                        "volume": int(float(r["成交量"])) if r.get("成交量") is not None else None,
                        "amount": float(r["成交额"]) if r.get("成交额") is not None else None,
                    }
                    if existing:
                        for k, v in vals.items():
                            setattr(existing, k, v)
                    else:
                        self.session.add(ConceptDaily(concept_code=c.code, trade_date=td, **vals))
                    row_count += 1

                self.session.commit()
                step("concept_daily", 1, label=f"{i+1}/{total} {c.name} ({len(df)}条)")
            except Exception as e:
                logger.warning("concept %s daily failed: %s", c.name, e)
                step("concept_daily", 1, label=f"{i+1}/{total} {c.name} (异常)")
                continue

        finish("concept_daily")
        return row_count
