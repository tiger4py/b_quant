"""每日增量更新 — 最近5个交易日的 stock K线 + concept 指数"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import time
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
    if datetime.now().hour < 17:
        today = today - timedelta(days=1)
    for d in reversed(dates):
        if d <= today:
            return d.strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")
    
def update_stocks():
    """增量更新最近 DAYS 个交易日的日K线"""
    if not _bs_login():
        return

    ref = _last_trade_date()
    start = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=DAYS * 2)).strftime("%Y-%m-%d")
    end = ref

    with Session() as sess:
        codes = [r[0] for r in sess.query(StockInfo.code)
                 .filter(StockInfo.type == "1", StockInfo.status == 1).all()]

        # 批量查询已存在的日K记录，构建 {code: {已存在日期}}
        existing_rows = sess.query(StockDaily.code, StockDaily.trade_date).filter(
            StockDaily.trade_date >= start,
            StockDaily.trade_date <= end,
        ).all()

    existing_by_code = {}
    for c, td in existing_rows:
        existing_by_code.setdefault(c, set()).add(td)

    # 预期交易日：DB中出现的日期 ∪ {最近交易日}（兜底首次运行）
    all_dates = set()
    for dates in existing_by_code.values():
        all_dates.update(dates)
    all_dates.add(ref)

    # 筛选需要更新的股票
    need_update = []
    for code in codes:
        missing = all_dates - existing_by_code.get(code, set())
        if missing:
            need_update.append(code)

    skip_count = len(codes) - len(need_update)
    logger.info("Updating stock daily: %d total, %d already updated, %d need update, %s ~ %s",
                len(codes), skip_count, len(need_update), start, end)

    if not need_update:
        bs.logout()
        logger.info("Stock daily all up to date, nothing to do")
        return

    count = 0
    total_codes = len(need_update)
    consecutive_failures = 0
    for idx, code in enumerate(need_update, start=1):
        started_at = time.perf_counter()
        if idx == 1 or idx % 200 == 1:
            logger.info("Processing stock %d/%d: %s", idx, total_codes, code)
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS,
                start_date=start, end_date=end,
                frequency=K_FREQUENCY, adjustflag="3"
            )
            if rs.error_code != "0":
                logger.warning("baostock query failed for %s: %s", code, rs.error_msg)
                consecutive_failures += 1
                if _should_reconnect(rs.error_msg) or consecutive_failures >= 5:
                    logger.warning("baostock connection looks broken, reconnecting before next stock")
                    bs.logout()
                    if not _bs_login():
                        return
                    consecutive_failures = 0
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue
            consecutive_failures = 0

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
            logger.exception("update stock failed: %s", code)
            consecutive_failures += 1
            if consecutive_failures >= 5:
                logger.warning("too many consecutive stock update failures, reconnecting baostock")
                bs.logout()
                if not _bs_login():
                    return
                consecutive_failures = 0
            continue
        finally:
            elapsed = time.perf_counter() - started_at
            if elapsed >= 10:
                logger.warning("Stock update slow: %s took %.2fs", code, elapsed)
            if idx % 50 == 0 or idx == total_codes:
                logger.info(
                    "Stock daily progress: %d/%d (%.1f%%), rows=%d, current=%s, elapsed=%.2fs",
                    idx,
                    total_codes,
                    idx / total_codes * 100 if total_codes else 100,
                    count,
                    code,
                    elapsed,
                )

    bs.logout()
    logger.info("Stock daily updated: %d rows, %d stocks skipped (already updated)", count, skip_count)


def _bs_login():
    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock login failed: %s", lg.error_msg)
        return False
    logger.info("baostock login ok")
    return True


def _should_reconnect(error_msg):
    if not error_msg:
        return False
    text = str(error_msg)
    keywords = [
        "网络接收错误",
        "接收数据异常",
        "10054",
        "远程主机强迫关闭了一个现有的连接",
        "连接",
    ]
    return any(word in text for word in keywords)


def update_concepts():
    """增量更新最近 DAYS 个交易日的概念指数（跳过已更新的）"""
    import akshare as ak
    from models.stock import Concept, ConceptDaily

    ref = _last_trade_date()
    start_date = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=DAYS * 2)).strftime("%Y%m%d")
    end_date = ref.replace("-", "")
    start_ymd = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=DAYS * 2)).strftime("%Y-%m-%d")

    with Session() as sess:
        concepts = sess.query(Concept).all()

        # 批量查询已存在的概念日线记录
        existing_rows = sess.query(ConceptDaily.concept_code, ConceptDaily.trade_date).filter(
            ConceptDaily.trade_date >= start_ymd,
            ConceptDaily.trade_date <= ref,
        ).all()

    # 构建 {concept_code: {已存在日期}}
    existing_by_code = {}
    for cc, td in existing_rows:
        existing_by_code.setdefault(cc, set()).add(td)

    # 预期交易日：DB中出现的日期 ∪ {最近交易日}（兜底首次运行）
    all_dates = set()
    for dates in existing_by_code.values():
        all_dates.update(dates)
    all_dates.add(ref)

    # 筛选需要更新的概念
    need_update = []
    for c in concepts:
        missing = all_dates - existing_by_code.get(c.code, set())
        if missing:
            need_update.append(c)

    skip_count = len(concepts) - len(need_update)
    logger.info("Updating concept daily: %d total, %d already updated, %d need update, %s ~ %s",
                len(concepts), skip_count, len(need_update), start_date, end_date)

    if not need_update:
        logger.info("Concept daily all up to date, nothing to do")
        return

    count = 0
    failed = 0
    total_concepts = len(need_update)

    for idx, c in enumerate(need_update, start=1):
        started_at = time.perf_counter()
        if idx == 1 or idx % 50 == 1:
            logger.info("Processing concept %d/%d: %s(%s)", idx, total_concepts, c.name, c.code)
        try:
            df = ak.stock_board_concept_index_ths(
                symbol=c.name,
                start_date=start_date,
                end_date=end_date,
            )
            if df.empty:
                logger.warning("concept daily empty: %s(%s)", c.name, c.code)
                continue
            df = df.tail(DAYS * 2)
            cols = list(df.columns)
            if len(cols) < 7:
                failed += 1
                logger.warning("concept daily columns unexpected for %s(%s): %s", c.name, c.code, cols)
                continue

            with Session.begin() as sess:
                for _, r in df.iterrows():
                    td = str(r.iloc[0])[:10]
                    existing = sess.query(ConceptDaily).filter_by(
                        concept_code=c.code, trade_date=td
                    ).first()
                    values = {
                        "open": float(r.iloc[1]) if r.iloc[1] is not None else None,
                        "high": float(r.iloc[2]) if r.iloc[2] is not None else None,
                        "low": float(r.iloc[3]) if r.iloc[3] is not None else None,
                        "close": float(r.iloc[4]) if r.iloc[4] is not None else None,
                        "volume": int(float(r.iloc[5])) if r.iloc[5] is not None else None,
                        "amount": float(r.iloc[6]) if r.iloc[6] is not None else None,
                    }
                    if existing:
                        for k, val in values.items():
                            setattr(existing, k, val)
                    else:
                        sess.add(ConceptDaily(concept_code=c.code, trade_date=td, **values))
                    count += 1
        except Exception:
            failed += 1
            logger.exception("update concept failed: %s(%s)", c.name, c.code)
            continue
        finally:
            elapsed = time.perf_counter() - started_at
            if elapsed >= 10:
                logger.warning("Concept update slow: %s(%s) took %.2fs", c.name, c.code, elapsed)
            if idx % 20 == 0 or idx == total_concepts:
                logger.info(
                    "Concept daily progress: %d/%d (%.1f%%), rows=%d, failed=%d, current=%s(%s), elapsed=%.2fs",
                    idx,
                    total_concepts,
                    idx / total_concepts * 100 if total_concepts else 100,
                    count,
                    failed,
                    c.name,
                    c.code,
                    elapsed,
                )

    logger.info("Concept daily updated: %d rows, failed=%d, skipped=%d (already updated)", count, failed, skip_count)


if __name__ == "__main__":
    logger.info("=== Daily update start ===")
    update_stocks()
    update_concepts()
    logger.info("=== Daily update done ===")
