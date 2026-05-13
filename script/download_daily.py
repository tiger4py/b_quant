"""后台下载日K线 — 单进程顺序版"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from datetime import datetime, timedelta

import baostock as bs
from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS, K_FIELDS, K_FREQUENCY
from logic.progress import start, step, finish
from models.stock import StockInfo, StockDaily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _make_engine():
    e = create_engine(DATABASE_URL, echo=False)
    @event.listens_for(e, "connect")
    def _wal(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA synchronous=NORMAL")
    return e


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


def download():
    # ── 登录 baostock ──
    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock login failed: %s", lg.error_msg)
        return

    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=DOWNLOAD_DAYS * 2)).strftime("%Y-%m-%d")
    ref_date = _last_trade_date()

    logger.info("Reference date: %s", ref_date)

    with Session() as sess:
        all_codes = [
            r[0] for r in sess.query(StockInfo.code)
            .filter(StockInfo.type == "1", StockInfo.status == 1).all()
        ]
        recent = dict(sess.query(
            StockDaily.code, func.max(StockDaily.trade_date)
        ).filter(
            StockDaily.code.in_(all_codes)
        ).group_by(StockDaily.code).all())

    need = [c for c in all_codes if c not in recent or recent[c] < ref_date]
    skipped = len(all_codes) - len(need)
    if skipped:
        logger.info("Skipping %d up-to-date, %d remaining", skipped, len(need))

    total = len(need)
    if total == 0:
        logger.info("All up to date")
        bs.logout()
        return

    logger.info("Downloading: %d stocks, %d days", total, DOWNLOAD_DAYS)
    start("daily", total, label="开始下载...")

    count = 0
    for i, code in enumerate(need):
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS,
                start_date=start_date, end_date=end_date,
                frequency=K_FREQUENCY, adjustflag="3"
            )
            if rs.error_code != "0":
                step("daily", 1, label=f"{i+1}/{total} {code} (错误)")
                continue

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                step("daily", 1, label=f"{i+1}/{total} {code} (空)")
                continue

            rows = rows[-DOWNLOAD_DAYS:] if len(rows) > DOWNLOAD_DAYS else rows

            with Session.begin() as sess:
                for row in rows:
                    existing = sess.query(StockDaily).filter_by(
                        code=code, trade_date=row[0],
                    ).first()
                    vals = {
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
                        for k, v in vals.items():
                            setattr(existing, k, v)
                    else:
                        sess.add(StockDaily(code=code, trade_date=row[0], **vals))
                    count += 1

            if (i + 1) % 20 == 0:
                pct = (i + 1) * 100 // total
                logger.info("%d/%d (%d%%) %d rows", i + 1, total, pct, count)
            step("daily", 1, label=f"{i+1}/{total} {code} ({len(rows)}条)")

        except UnicodeDecodeError:
            step("daily", 1, label=f"{i+1}/{total} {code} (编码)")
        except Exception as e:
            step("daily", 1, label=f"{i+1}/{total} {code} (异常)")
            logger.warning("%s: %s", code, e)

    bs.logout()
    engine.dispose()
    finish("daily")
    logger.info("DONE: %d stocks, %d rows", total, count)


if __name__ == "__main__":
    download()
