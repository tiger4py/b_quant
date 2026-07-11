# -*- coding: utf-8 -*-
"""
同花顺概念指数数据采集 — AKShare 源，按月归档: data/concept/{year}/YYYY-MM.csv

借鉴 update_etf.py 的分批/冷却/增量保存模式，
使用 update_daily.py 的 AKShare 概念指数获取方式。

用法:
    python script/update_concept_ths.py                          # 拉取最近交易日
    python script/update_concept_ths.py --date 2026-07-04        # 指定日期
    python script/update_concept_ths.py --start 2026-06-19 --end 2026-07-04  # 日期范围
    python script/update_concept_ths.py --mode init              # 首次：拉近2年历史
    python script/update_concept_ths.py --codes 300800,309121    # 只拉指定概念
"""

import os
import sys
import csv
import json
import time
import argparse
import logging
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONCEPT_DIR = os.path.join(ROOT_DIR, "data", "concept")

# ======== 参数 ========
BATCH_SIZE = 20
BATCH_COOLDOWN = 30  # 批次间冷却秒数
REQUEST_INTERVAL = 1.0  # 单个概念请求间隔
FIELD_NAMES = ["concept_code", "concept_name", "trade_date",
               "open", "high", "low", "close", "volume", "amount"]

# ======== 概念列表 ========


def _get_concept_list(codes_filter=None):
    """从数据库获取概念列表，返回 [(code, name), ...]。

    参数:
        codes_filter: 可选的概念代码列表，只下载指定概念
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    from models.stock import Concept

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)

    with Session() as sess:
        query = sess.query(Concept)
        if codes_filter:
            query = query.filter(Concept.code.in_(codes_filter))
        concepts = [(c.code, c.name) for c in query.all()]

    logger.info("概念列表: %d 个", len(concepts))
    return concepts


# ======== 文件读写 ========


def _month_path(month_str):
    """month_str='2026-07' → data/concept/2026/2026-07.csv"""
    year = month_str[:4]
    d = os.path.join(CONCEPT_DIR, year)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{month_str}.csv")


def _load_existing_month(month_str):
    """读取已有月度 CSV，返回 {concept_code: {trade_date: row_dict}}"""
    fp = _month_path(month_str)
    existing = defaultdict(dict)
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                existing[row["concept_code"]][row["trade_date"]] = row
    return existing


def _save_months(rows_by_month):
    """按年月写入 CSV: data/concept/{year}/YYYY-MM.csv

    读取已有数据 → 合并新数据 → 按 code+date 排序写回
    """
    for month_str in sorted(rows_by_month):
        fp = _month_path(month_str)
        existing = _load_existing_month(month_str)

        # 合并：新数据覆盖已有
        for code, date_rows in rows_by_month[month_str].items():
            for date_str, row in date_rows.items():
                existing[code][date_str] = row

        # 展平并排序
        all_rows = []
        for code in sorted(existing):
            all_rows.extend(existing[code][d] for d in sorted(existing[code]))

        if all_rows:
            with open(fp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=FIELD_NAMES)
                w.writeheader()
                w.writerows(all_rows)
            logger.info("  -> %s: %d 行 (%d 概念)", fp, len(all_rows), len(existing))


# ======== 数据获取 ========


def _fetch_concept_daily(code, name, start_date, end_date):
    """用 AKShare 拉取单个概念指数的日线数据。

    参数:
        code: 概念代码，如 '300800'
        name: 概念名称，如 '阿里巴巴概念'
        start_date: 'YYYYMMDD' 格式起始日期
        end_date: 'YYYYMMDD' 格式结束日期

    返回:
        [{trade_date, open, high, low, close, volume, amount}, ...] 或空列表
    """
    import akshare as ak

    try:
        df = ak.stock_board_concept_index_ths(
            symbol=name,
            start_date=start_date,
            end_date=end_date,
        )
        if df is None or df.empty:
            return []

        cols = list(df.columns)
        if len(cols) < 7:
            logger.warning("概念 %s(%s) 列数不足: %s", code, name, cols)
            return []

        # 过滤到请求的日期范围
        df[df.columns[0]] = df.iloc[:, 0].astype(str)
        mask = df.iloc[:, 0].str[:10].between(
            datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d"),
            datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d"),
        )
        df = df[mask]

        rows = []
        for _, r in df.iterrows():
            td = str(r.iloc[0])[:10]
            v = float(r.iloc[5]) if len(cols) > 5 and r.iloc[5] is not None else None
            rows.append({
                "concept_code": code,
                "concept_name": name,
                "trade_date": td,
                "open": float(r.iloc[1]) if r.iloc[1] is not None else None,
                "high": float(r.iloc[2]) if r.iloc[2] is not None else None,
                "low": float(r.iloc[3]) if r.iloc[3] is not None else None,
                "close": float(r.iloc[4]) if r.iloc[4] is not None else None,
                "volume": int(v) if v is not None else None,
                "amount": float(r.iloc[6]) if len(cols) > 6 and r.iloc[6] is not None else None,
            })
        return rows

    except Exception:
        logger.exception("拉取失败: %s(%s)", code, name)
        return []


def _process_batch(batch, start_date, end_date):
    """处理一批概念，返回 (rows_by_month, row_count, fail_count)。

    rows_by_month: {month_str: {concept_code: {trade_date: row_dict}}}
    """
    rows_by_month = defaultdict(lambda: defaultdict(dict))
    row_count = 0
    fail_count = 0

    for code, name in batch:
        rows = _fetch_concept_daily(code, name, start_date, end_date)
        if not rows:
            fail_count += 1
        else:
            for row in rows:
                month = row["trade_date"][:7]
                rows_by_month[month][code][row["trade_date"]] = row
                row_count += 1

        time.sleep(REQUEST_INTERVAL)

    return rows_by_month, row_count, fail_count


# ======== 主流程 ========


def download_concepts(start_date, end_date, codes_filter=None):
    """拉取概念指数日线，分批避免限流。

    参数:
        start_date, end_date: 'YYYY-MM-DD' 格式日期范围
        codes_filter: 可选的概念代码列表
    """
    concepts = _get_concept_list(codes_filter=codes_filter)
    total = len(concepts)

    if total == 0:
        logger.warning("没有找到概念")
        return

    # AKShare 需要 YYYYMMDD 格式
    start_fmt = start_date.replace("-", "")
    end_fmt = end_date.replace("-", "")

    batches = [concepts[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    total_rows = 0
    total_fails = 0

    logger.info("=== 概念数据采集: %s ~ %s, 共 %d 概念, 分 %d 批 ===",
                start_date, end_date, total, len(batches))

    for batch_idx, batch in enumerate(batches):
        batch_start = time.time()

        name_preview = ", ".join(f"{c}({n})" for c, n in batch[:3])
        logger.info("批次 %d/%d: %s ...", batch_idx + 1, len(batches), name_preview)

        rows_by_month, row_count, fail_count = _process_batch(
            batch, start_fmt, end_fmt
        )

        total_rows += row_count
        total_fails += fail_count

        # 每批结束就保存（增量写入，不怕中断）
        _save_months(rows_by_month)

        pct = (batch_idx + 1) / len(batches) * 100
        elapsed = time.time() - batch_start
        logger.info("批次 %d/%d (%.0f%%): rows=%d, fails=%d, 耗时 %.0fs",
                    batch_idx + 1, len(batches), pct,
                    row_count, fail_count, elapsed)

        # 批次间冷却（最后一批不用等）
        if batch_idx < len(batches) - 1:
            logger.info("冷却 %ds ...", BATCH_COOLDOWN)
            time.sleep(BATCH_COOLDOWN)

    logger.info("=== 完成: %d 概念, %d 行, %d 失败 ===", total, total_rows, total_fails)


# ======== CLI ========


def _resolve_date_range(args, parser=None):
    if args.date and (args.start or args.end):
        if parser is not None:
            parser.error("--date 不能和 --start/--end 同时使用")
        raise ValueError("--date 不能和 --start/--end 同时使用")

    if args.mode == "init":
        from datetime import timedelta
        start = args.start or (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        end = args.end or datetime.now().strftime("%Y-%m-%d")
    elif args.start or args.end:
        start = args.start or args.end
        end = args.end or args.start
    else:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        start = end = target

    datetime.strptime(start, "%Y-%m-%d")
    datetime.strptime(end, "%Y-%m-%d")
    if start > end:
        if parser is not None:
            parser.error("--start 不能晚于 --end")
        raise ValueError("--start 不能晚于 --end")
    return start, end


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同花顺概念指数数据采集（AKShare 源）")
    parser.add_argument("--date", default=None,
                        help="指定日期 YYYY-MM-DD")
    parser.add_argument("--mode", choices=["daily", "init"], default="daily",
                        help="daily=增量, init=历史全量")
    parser.add_argument("--start", default=None,
                        help="起始日期 YYYY-MM-DD；可配合 --end 拉取日期范围")
    parser.add_argument("--end", default=None,
                        help="结束日期 YYYY-MM-DD；可配合 --start 拉取日期范围")
    parser.add_argument("--codes", default=None,
                        help="只下载指定概念代码，逗号分隔")
    args = parser.parse_args()

    codes_filter = None
    if args.codes:
        codes_filter = [c.strip() for c in args.codes.split(",") if c.strip()]

    start, end = _resolve_date_range(args, parser)
    download_concepts(start, end, codes_filter=codes_filter)
