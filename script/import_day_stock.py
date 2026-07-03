"""将 data/day_stock/ 和 data/day_concept/ 下的 CSV 批量导入数据库

用法:
    # 导入最近7天（默认）
    python script/import_day_stock.py

    # 导入指定日期
    python script/import_day_stock.py --date 2026-07-03

    # 导入日期范围
    python script/import_day_stock.py --start 2026-07-01 --end 2026-07-03

    # 导入所有历史
    python script/import_day_stock.py --all

    # 只导入 stock / concept
    python script/import_day_stock.py --type stock
    python script/import_day_stock.py --date 2026-07-03 --type concept

    # 静默模式
    python script/import_day_stock.py -q

CSV 格式（由 update_daily.py 生成）:
    stock:  code,trade_date,open,high,low,close,volume,amount,turn,pe_ttm
    concept: concept_code,trade_date,open,high,low,close,volume,amount

导入策略: INSERT OR REPLACE，可重复执行不报错。
"""
import os, sys, csv, glob, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
from datetime import datetime, date, timedelta
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL, DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ======== 默认目录 ========
DAY_STOCK_DIR = os.path.join(DATA_DIR, "day_stock")
DAY_CONCEPT_DIR = os.path.join(DATA_DIR, "day_concept")

# ======== 表结构 ========
STOCK_COLUMNS = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "turn", "pe_ttm"]
CONCEPT_COLUMNS = ["concept_code", "trade_date", "open", "high", "low", "close", "volume", "amount"]

# INSERT OR REPLACE 模板
# SQLite 支持 INSERT OR REPLACE，利用唯一索引自动去重
STOCK_INSERT_SQL = """
INSERT OR REPLACE INTO stock_daily (code, trade_date, open, high, low, close, volume, amount, turn, pe_ttm)
VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, :turn, :pe_ttm)
"""

CONCEPT_INSERT_SQL = """
INSERT OR REPLACE INTO concept_daily (concept_code, trade_date, open, high, low, close, volume, amount)
VALUES (:concept_code, :trade_date, :open, :high, :low, :close, :volume, :amount)
"""


def _find_csv_files(directory: str) -> list:
    """递归查找目录下所有 CSV 文件，按路径排序"""
    if not os.path.isdir(directory):
        logger.warning("Directory not found: %s", directory)
        return []
    files = glob.glob(os.path.join(directory, "**/*.csv"), recursive=True)
    files.sort()
    return files


def _filter_by_date(files: list, start_date: str, end_date: str) -> list:
    """按日期范围过滤 CSV 文件。

    CSV 文件名格式: YYYY-MM-DD.csv（如 2026-07-03.csv）
    只保留文件名日期在 [start_date, end_date] 范围内的文件。
    """
    if not start_date and not end_date:
        return files

    date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})\.csv$')
    filtered = []
    for f in files:
        m = date_pattern.search(f)
        if not m:
            # 文件名不含日期，保留（可能是 _tmp 目录下的临时文件）
            continue
        file_date = m.group(1)
        if start_date and file_date < start_date:
            continue
        if end_date and file_date > end_date:
            continue
        filtered.append(f)
    return filtered


def _today_str() -> str:
    """返回今天的日期字符串 YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    """返回 N 天前的日期字符串 YYYY-MM-DD"""
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _read_csv(filepath: str) -> list:
    """读取 CSV 文件，返回 dict 列表。跳过空行和无效行。"""
    rows = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 跳过空行
            if not any(v for v in row.values()):
                continue
            rows.append(row)
    return rows


def _parse_stock_row(row: dict) -> dict:
    """将 CSV 行转为 INSERT 用的参数字典，处理类型转换"""
    def _float(v):
        if v is None or v == "" or v == "None":
            return None
        return float(v)

    def _int(v):
        if v is None or v == "" or v == "None":
            return None
        return int(float(v))

    return {
        "code": row["code"],
        "trade_date": row["trade_date"],
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        "close": _float(row.get("close")),
        "volume": _int(row.get("volume")),
        "amount": _float(row.get("amount")),
        "turn": _float(row.get("turn")),
        "pe_ttm": _float(row.get("pe_ttm")),
    }


def _parse_concept_row(row: dict) -> dict:
    """将 CSV 行转为 INSERT 用的参数字典"""
    def _float(v):
        if v is None or v == "" or v == "None":
            return None
        return float(v)

    def _int(v):
        if v is None or v == "" or v == "None":
            return None
        return int(float(v))

    return {
        "concept_code": row["concept_code"],
        "trade_date": row["trade_date"],
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        "close": _float(row.get("close")),
        "volume": _int(row.get("volume")),
        "amount": _float(row.get("amount")),
    }


def import_stock(engine, directory: str = None, start_date: str = None, end_date: str = None) -> int:
    """导入 stock_daily CSV，返回导入行数。

    参数:
        directory: CSV 根目录，默认 DAY_STOCK_DIR
        start_date: 起始日期 YYYY-MM-DD（可选）
        end_date: 截止日期 YYYY-MM-DD（可选）
    """
    if directory is None:
        directory = DAY_STOCK_DIR

    files = _find_csv_files(directory)
    if start_date or end_date:
        files = _filter_by_date(files, start_date, end_date)

    if not files:
        logger.info("No stock CSV files found in %s (date filter: %s ~ %s)", directory, start_date or '-', end_date or '-')
        return 0

    logger.info("Importing stock daily from %d files in %s", len(files), directory)
    total = 0

    with engine.begin() as conn:
        for filepath in files:
            rows = _read_csv(filepath)
            if not rows:
                continue

            params = [_parse_stock_row(r) for r in rows]
            conn.execute(text(STOCK_INSERT_SQL), params)
            total += len(params)
            logger.info("  %s: %d rows", os.path.basename(filepath), len(params))

    return total


def import_concept(engine, directory: str = None, start_date: str = None, end_date: str = None) -> int:
    """导入 concept_daily CSV，返回导入行数。

    参数:
        directory: CSV 根目录，默认 DAY_CONCEPT_DIR
        start_date: 起始日期 YYYY-MM-DD（可选）
        end_date: 截止日期 YYYY-MM-DD（可选）
    """
    if directory is None:
        directory = DAY_CONCEPT_DIR

    files = _find_csv_files(directory)
    if start_date or end_date:
        files = _filter_by_date(files, start_date, end_date)

    if not files:
        logger.info("No concept CSV files found in %s (date filter: %s ~ %s)", directory, start_date or '-', end_date or '-')
        return 0

    logger.info("Importing concept daily from %d files in %s", len(files), directory)
    total = 0

    with engine.begin() as conn:
        for filepath in files:
            rows = _read_csv(filepath)
            if not rows:
                continue

            params = [_parse_concept_row(r) for r in rows]
            conn.execute(text(CONCEPT_INSERT_SQL), params)
            total += len(params)
            logger.info("  %s: %d rows", os.path.basename(filepath), len(params))

    return total


def main():
    parser = argparse.ArgumentParser(description="导入 day_stock / day_concept CSV 到数据库")
    parser.add_argument("--type", choices=["stock", "concept"], default=None,
                        help="只导入指定类型 (默认: 全部)")
    parser.add_argument("--date", default=None,
                        help="导入指定日期 YYYY-MM-DD (默认: 最近7天)")
    parser.add_argument("--start", default=None,
                        help="起始日期 YYYY-MM-DD (需配合 --end)")
    parser.add_argument("--end", default=None,
                        help="截止日期 YYYY-MM-DD (需配合 --start)")
    parser.add_argument("--all", action="store_true",
                        help="导入所有历史 CSV（不限制日期）")
    parser.add_argument("--path", default=None,
                        help="指定 CSV 目录 (覆盖默认路径)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="静默模式，只显示汇总")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # 确定日期范围
    if args.all:
        start_date = None
        end_date = None
    elif args.start or args.end:
        start_date = args.start
        end_date = args.end
    elif args.date:
        start_date = args.date
        end_date = args.date
    else:
        # 默认：导入最近7天
        today = _today_str()
        start_date = _days_ago(6)
        end_date = today

    # --path 模式下不应用日期过滤（用户明确指定了目录）
    use_date_filter = not args.path

    engine = create_engine(DATABASE_URL, echo=False)

    @event.listens_for(engine, "connect")
    def _wal(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    total_stock = 0
    total_concept = 0

    sd = start_date if use_date_filter else None
    ed = end_date if use_date_filter else None

    if args.path:
        # 指定了路径，智能判断类型
        path_lower = args.path.lower()
        if args.type == "concept" or "concept" in path_lower:
            total_concept = import_concept(engine, args.path, sd, ed)
        else:
            total_stock = import_stock(engine, args.path, sd, ed)
    else:
        if args.type is None or args.type == "stock":
            total_stock = import_stock(engine, start_date=sd, end_date=ed)
        if args.type is None or args.type == "concept":
            total_concept = import_concept(engine, start_date=sd, end_date=ed)

    logger.info("=== Import done: stock=%d, concept=%d ===", total_stock, total_concept)


if __name__ == "__main__":
    main()
