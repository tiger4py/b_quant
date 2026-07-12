"""每日增量数据采集 — 拉取 stock K线 + concept 指数，存为 CSV

用法:
    python script/update_daily.py                      # 当日（非交易日则跳过）
    python script/update_daily.py --date 2026-06-23     # 指定日期
    python script/update_daily.py --start 2026-01-01 --end 2026-06-23  # 日期范围

输出目录:
    data/day_stock/YYYYMM/YYYY-MM-DD.csv   — 股票日K线
    data/day_concept/YYYYMM/YYYY-MM-DD.csv — 概念指数日线

不再直接写数据库，数据库导入由 script/import_day_stock.py 负责。
"""
import os, sys, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import argparse
import logging
import time
from datetime import datetime
from collections import defaultdict

import baostock as bs

from config import DATA_DIR, K_FIELDS, K_FREQUENCY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ======== 输出目录 ========
DAY_STOCK_DIR = os.path.join(DATA_DIR, "day_stock")
DAY_CONCEPT_DIR = os.path.join(DATA_DIR, "day_concept")


_TRADE_DATES_CACHE = None  # 交易日集合缓存


def _get_trade_dates() -> set:
    """获取 A 股交易日集合（带缓存），格式 {'2026-06-23', ...}"""
    global _TRADE_DATES_CACHE
    if _TRADE_DATES_CACHE is not None:
        return _TRADE_DATES_CACHE
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    _TRADE_DATES_CACHE = set(str(d) for d in sorted(df["trade_date"].tolist()))
    return _TRADE_DATES_CACHE


def _is_trade_date(date_str: str) -> bool:
    """判断 date_str (YYYY-MM-DD) 是否为交易日"""
    return date_str in _get_trade_dates()


def _resolve_dates(args) -> tuple:
    """根据命令行参数解析 (start, end)，返回 (str, str) 或 (None, None) 表示跳过"""
    if args.date:
        start = end = args.date
    elif args.start and args.end:
        start, end = args.start, args.end
    elif args.start:
        # 只有 start，end 用当日
        start = args.start
        end = datetime.now().strftime("%Y-%m-%d")
    else:
        # 默认：自动判断拉哪天
        #   - 交易日且已过17点 → 拉今天
        #   - 交易日但未到17点 → 拉昨天（数据还没出）
        #   - 非交易日 → 往前找最近缺失CSV的交易日
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        trade_dates = _get_trade_dates()
        from datetime import timedelta

        if _is_trade_date(today) and now.hour >= 17:
            start = end = today
        else:
            if _is_trade_date(today):
                logger.info("今日 %s 是交易日但未到17点，回退到昨天", today)
            else:
                logger.info("今日 %s 非交易日，寻找最近交易日...", today)
            # 往前找最近一个 CSV 缺失或不完整的交易日
            found = None
            for offset in range(1, 11):
                d = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
                if d not in trade_dates:
                    continue
                month_dir = os.path.join(DAY_STOCK_DIR, d[:7].replace("-", ""))
                filepath = os.path.join(month_dir, f"{d}.csv")
                if not os.path.isfile(filepath):
                    found = d
                    break
                try:
                    with open(filepath, "r", encoding="utf-8-sig") as f:
                        line_count = sum(1 for _ in f) - 1
                    if line_count < 4000:
                        found = d
                        break
                except Exception:
                    pass
            if found:
                logger.info("找到缺失数据的交易日: %s", found)
                start = end = found
            else:
                logger.info("最近交易日数据已齐全，无需更新")
                return None, None

    # 校验日期格式
    for label, d in [("start", start), ("end", end)]:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            logger.error("日期格式错误 (%s): %s，应为 YYYY-MM-DD", label, d)
            return None, None

    return start, end


def _write_csv_by_date(rows_by_date: dict, base_dir: str):
    """将 {date: [row_dicts]} 写入 base_dir/YYYYMM/YYYY-MM-DD.csv"""
    for trade_date, rows in rows_by_date.items():
        if not rows:
            continue
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        month_dir = os.path.join(base_dir, dt.strftime("%Y%m"))
        os.makedirs(month_dir, exist_ok=True)
        filepath = os.path.join(month_dir, f"{trade_date}.csv")

        # 按 code 排序，保证输出稳定
        rows.sort(key=lambda r: r.get("code", ""))

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        logger.info("  -> saved %d rows to %s", len(rows), filepath)


def _existing_csv_dates(start: str, end: str, expected_min: int = 4000) -> set:
    """检查日期范围内已有的 CSV 文件，跳过数据完整的日期。

    CSV 文件路径: data/day_stock/YYYYMM/YYYY-MM-DD.csv
    expected_min: CSV 最少行数才算完整（默认 4000 只）
    返回: 已存在且完整的日期集合
    """
    existing = set()
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    from datetime import timedelta
    d = start_dt
    while d <= end_dt:
        date_str = d.strftime("%Y-%m-%d")
        month_dir = os.path.join(DAY_STOCK_DIR, d.strftime("%Y%m"))
        filepath = os.path.join(month_dir, f"{date_str}.csv")
        if os.path.isfile(filepath):
            # 数一下行数（减去表头），够 expected_min 就算完整
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    line_count = sum(1 for _ in f) - 1  # 减表头
                if line_count >= expected_min:
                    existing.add(date_str)
            except Exception:
                pass
        d += timedelta(days=1)
    return existing


def update_stocks(start: str, end: str):
    """拉取指定日期范围的日K线，逐只追加到单个临时文件，最后按日期拆分。

    中途断连不丢数据：已拉取的股票追加在 _tmp/{start}_{end}.csv 中，重跑时自动跳过。
    """
    import shutil
    from datetime import timedelta

    if not _bs_login():
        return

    # 获取所有正常上市股票
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    from models.stock import StockInfo

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)

    with Session() as sess:
        codes = [r[0] for r in sess.query(StockInfo.code)
                 .filter(StockInfo.type == "1", StockInfo.status == 1).all()]

    # 检查已有最终 CSV，跳过完整的日期
    existing = _existing_csv_dates(start, end, expected_min=len(codes) - 20)
    if existing:
        logger.info("跳过已有完整数据的日期 (%d 天): %s", len(existing),
                    ", ".join(sorted(existing)))
    total_days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
    if len(existing) >= total_days:
        logger.info("所有日期已有完整数据，跳过拉取")
        return

    FIELD_NAMES = ["code", "trade_date", "open", "high", "low", "close",
                   "volume", "amount", "turn", "pe_ttm"]

    # ★ 单个临时文件，追加写，断连不丢
    tmp_dir = os.path.join(DAY_STOCK_DIR, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"{start}_{end}.csv")

    # 断点续跑：读取临时文件中已完成的 code
    done_codes = set()
    if os.path.exists(tmp_path):
        with open(tmp_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                done_codes.add(row.get("code", ""))
        if done_codes:
            logger.info("断点续跑: 临时文件已有 %d 只股票", len(done_codes))

    pending_codes = [c for c in codes if c not in done_codes]

    if not pending_codes:
        logger.info("所有股票已在临时文件中，跳过拉取，直接拆分")
        bs.logout()
        _split_tmp_to_dates(tmp_path, DAY_STOCK_DIR)
        return

    logger.info("Fetching stock daily: %d stocks, %s ~ %s", len(pending_codes), start, end)

    # 打开临时文件（追加模式），全程只开一次
    tmp_exists = os.path.exists(tmp_path)
    with open(tmp_path, "a", newline="", encoding="utf-8-sig") as tmp_f:
        writer = csv.DictWriter(tmp_f, fieldnames=FIELD_NAMES)
        if not tmp_exists:
            writer.writeheader()
            tmp_f.flush()

        count = 0
        total_codes = len(pending_codes)
        consecutive_failures = 0

        for idx, code in enumerate(pending_codes, start=1):
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
                            logger.info("连接失败，已拉取数据在 %s，重跑可续", tmp_path)
                            return
                        consecutive_failures = 0
                    continue

                stock_rows = []
                while rs.next():
                    stock_rows.append(rs.get_row_data())

                if not stock_rows:
                    # 无数据也记一行（code + 空日期），下次跳过
                    writer.writerow({"code": code, "trade_date": ""})
                    tmp_f.flush()
                    continue
                consecutive_failures = 0

                # 逐行写入临时文件
                for row in stock_rows:
                    row_dict = {
                        "code": code,
                        "trade_date": row[0],
                        "open": float(row[1]) if row[1] else None,
                        "high": float(row[2]) if row[2] else None,
                        "low": float(row[3]) if row[3] else None,
                        "close": float(row[4]) if row[4] else None,
                        "volume": int(float(row[5])) if row[5] else None,
                        "amount": float(row[6]) if row[6] else None,
                        "turn": float(row[7]) if row[7] else None,
                        "pe_ttm": float(row[8]) if row[8] else None,
                    }
                    writer.writerow(row_dict)
                    count += 1

                tmp_f.flush()  # ★ 每只股票刷盘，断连不丢

            except Exception:
                logger.exception("update stock failed: %s", code)
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    logger.warning("too many consecutive failures, reconnecting baostock")
                    bs.logout()
                    if not _bs_login():
                        logger.info("连接失败，已拉取数据在 %s，重跑可续", tmp_path)
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
                        idx, total_codes,
                        idx / total_codes * 100 if total_codes else 100,
                        count, code, elapsed,
                    )

    bs.logout()

    # 全部拉完 → 按日期拆分临时文件为最终 CSV
    _split_tmp_to_dates(tmp_path, DAY_STOCK_DIR)
    logger.info("Stock daily fetched: %d rows across %d stocks", count, total_codes)


def _split_tmp_to_dates(tmp_path: str, out_dir: str):
    """读取单个临时 CSV，按 trade_date 分组写入最终 CSV，然后删临时文件。"""
    import shutil

    rows_by_date = defaultdict(list)
    count = 0
    with open(tmp_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            td = row.get("trade_date", "")
            if td:
                rows_by_date[td].append(row)
                count += 1

    logger.info("拆分临时文件: %d 行 → %d 个日期", count, len(rows_by_date))
    _write_csv_by_date(dict(rows_by_date), out_dir)

    # 删临时文件
    os.remove(tmp_path)
    # 如果 _tmp 目录为空也删掉
    tmp_dir = os.path.dirname(tmp_path)
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    logger.info("临时文件已清理")


def update_concepts(start: str, end: str):
    """拉取指定日期范围的概念指数，按日期存 CSV"""
    import akshare as ak
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    from models.stock import Concept

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)

    # AKShare 需要 YYYYMMDD 格式
    start_date = start.replace("-", "")
    end_date = end.replace("-", "")

    with Session() as sess:
        concepts = sess.query(Concept).all()

    logger.info("Fetching concept daily: %d concepts, %s ~ %s", len(concepts), start_date, end_date)

    rows_by_date = defaultdict(list)
    count = 0
    failed = 0
    total_concepts = len(concepts)

    for idx, c in enumerate(concepts, start=1):
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
            # 过滤到请求的日期范围，避免 AKShare 返回多余数据
            df = df[df.iloc[:, 0].astype(str).str[:10].between(start, end)]
            if df.empty:
                continue
            cols = list(df.columns)
            if len(cols) < 7:
                failed += 1
                logger.warning("concept daily columns unexpected for %s(%s): %s", c.name, c.code, cols)
                continue

            for _, r in df.iterrows():
                td = str(r.iloc[0])[:10]
                row_dict = {
                    "concept_code": c.code,
                    "trade_date": td,
                    "open": float(r.iloc[1]) if r.iloc[1] is not None else None,
                    "high": float(r.iloc[2]) if r.iloc[2] is not None else None,
                    "low": float(r.iloc[3]) if r.iloc[3] is not None else None,
                    "close": float(r.iloc[4]) if r.iloc[4] is not None else None,
                    "volume": int(float(r.iloc[5])) if r.iloc[5] is not None else None,
                    "amount": float(r.iloc[6]) if r.iloc[6] is not None else None,
                }
                rows_by_date[td].append(row_dict)
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
                    idx, total_concepts,
                    idx / total_concepts * 100 if total_concepts else 100,
                    count, failed, c.name, c.code, elapsed,
                )

    _write_csv_by_date(rows_by_date, DAY_CONCEPT_DIR)
    logger.info("Concept daily fetched: %d rows across %d dates, failed=%d",
                count, len(rows_by_date), failed)


# ======== BaoStock 连接 ========

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="拉取 A 股日K线 + 概念指数，存为 CSV")
    parser.add_argument("--date", default=None,
                        help="指定日期 YYYY-MM-DD（默认: 当日，非交易日跳过）")
    parser.add_argument("--start", default=None,
                        help="起始日期 YYYY-MM-DD（需配合 --end）")
    parser.add_argument("--end", default=None,
                        help="结束日期 YYYY-MM-DD（需配合 --start）")
    parser.add_argument("--type", choices=["stock", "concept", "all"], default="stock",
                        help="数据类型 (默认: stock)")
    args = parser.parse_args()

    start, end = _resolve_dates(args)
    if start is None:
        sys.exit(0)

    logger.info("=== Daily update: %s ~ %s ===", start, end)
    if args.type in ("stock", "all"):
        update_stocks(start, end)
    if args.type in ("concept", "all"):
        update_concepts(start, end)
    logger.info("=== Daily update done ===")
