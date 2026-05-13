"""每日增量更新 — 最近5个交易日的 stock K线 + concept 指数"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from datetime import datetime, timedelta

import baostock as bs
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL, K_FIELDS, K_FREQUENCY
from models.stock import StockInfo, StockDaily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DAYS = 2

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)

@event.listens_for(engine, "connect")
def _wal(dbapi_connection, _):
    dbapi_connection.execute("PRAGMA journal_mode=WAL")


def _last_trade_date() -> str:
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    dates = sorted(df["trade_date"].tolist())
    today = datetime.now().date()
    if datetime.now().hour < 15:
        today = today - timedelta(days=1)
    for d in reversed(dates):
        if d <= today:
            return d.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def update_stocks():
    """增量更新最近 DAYS 个交易日的日K线"""
    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock login failed: %s", lg.error_msg)
        return

    ref = _last_trade_date()
    start = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=DAYS * 2)).strftime("%Y-%m-%d")
    end = ref

    with Session() as sess:
        codes = [r[0] for r in sess.query(StockInfo.code)
                 .filter(StockInfo.type == "1", StockInfo.status == 1).all()]

    logger.info("Updating stock daily: %d stocks, %s ~ %s", len(codes), start, end)
    count = 0
    end_date = _last_trade_date().replace("-", "")

    for code in codes:
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS,
                start_date=start, end_date=end,
                frequency=K_FREQUENCY, adjustflag="3"
            )
            if rs.error_code != "0":
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue

            with Session.begin() as sess:
                for row in rows:
                    existing = sess.query(StockDaily).filter_by(
                        code=code, trade_date=row[0]).first()
                    v = {
                        "open": float(row[1]) if row[1] else None,
                        "high": float(row[2]) if row[2] else None,
                        "low": float(row[3]) if row[3] else None,
                        "close": float(row[4]) if row[4] else None,
                        "volume": int(float(row[5])) if row[5] else None,
                        "amount": float(row[6]) if row[6] else None,
                        "turn": float(row[7]) if row[7] else None,
                        "pe_ttm": float(row[8]) if row[8] else None,
                    }
                    if existing:
                        for k, val in v.items():
                            setattr(existing, k, val)
                    else:
                        sess.add(StockDaily(code=code, trade_date=row[0], **v))
                    count += 1
        except Exception:
            continue

    bs.logout()
    logger.info("Stock daily updated: %d rows", count)


def update_concepts():
    """增量更新最近 DAYS 个交易日的概念指数"""
    import akshare as ak
    from models.stock import Concept, ConceptDaily

    with Session() as sess:
        concepts = sess.query(Concept).all()

    logger.info("Updating concept daily: %d concepts", len(concepts))
    count = 0
    end_date = _last_trade_date().replace("-", "")

    for c in concepts:
        try:
            df = ak.stock_board_concept_index_ths(symbol=c.name, end_date=end_date)
            if df.empty:
                continue
            df = df.tail(DAYS * 2)  # enough to cover recent trading days

            with Session.begin() as sess:
                for _, r in df.iterrows():
                    td = str(r["日期"])[:10]
                    existing = sess.query(ConceptDaily).filter_by(
                        concept_code=c.code, trade_date=td).first()
                    v = {
                        "open": float(r["开盘价"]) if r.get("开盘价") is not None else None,
                        "high": float(r["最高价"]) if r.get("最高价") is not None else None,
                        "low": float(r["最低价"]) if r.get("最低价") is not None else None,
                        "close": float(r["收盘价"]) if r.get("收盘价") is not None else None,
                        "volume": int(float(r["成交量"])) if r.get("成交量") is not None else None,
                        "amount": float(r["成交额"]) if r.get("成交额") is not None else None,
                    }
                    if existing:
                        for k, val in v.items():
                            setattr(existing, k, val)
                    else:
                        sess.add(ConceptDaily(concept_code=c.code, trade_date=td, **v))
                    count += 1
        except Exception:
            continue

    logger.info("Concept daily updated: %d rows", count)


if __name__ == "__main__":
    logger.info("=== Daily update start ===")
    update_stocks()
    update_concepts()
    logger.info("=== Daily update done ===")
