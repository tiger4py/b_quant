"""将 data/day_stock/ 和 data/day_concept/ 下的 CSV 批量导入数据库

用法:
    # 导入所有（stock + concept）
    python script/import_day_stock.py

    # 只导入 stock
    python script/import_day_stock.py --type stock

    # 只导入 concept
    python script/import_day_stock.py --type concept

    # 指定 CSV 目录（覆盖默认）
    python script/import_day_stock.py --path data/day_stock/202606/

    # 静默模式
    python script/import_day_stock.py -q

CSV 格式（由 update_daily.py 生成）:
    stock:  code,trade_date,open,high,low,close,volume,amount,turn,pe_ttm
    concept: concept_code,trade_date,open,high,low,close,volume,amount

导入策略: INSERT OR REPLACE，可重复执行不报错。
"""
import os, sys, csv, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
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


def import_stock(engine, directory: str = None) -> int:
    """导入 stock_daily CSV，返回导入行数"""
    if directory is None:
        directory = DAY_STOCK_DIR

    files = _find_csv_files(directory)
    if not files:
        logger.info("No stock CSV files found in %s", directory)
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


def import_concept(engine, directory: str = None) -> int:
    """导入 concept_daily CSV，返回导入行数"""
    if directory is None:
        directory = DAY_CONCEPT_DIR

    files = _find_csv_files(directory)
    if not files:
        logger.info("No concept CSV files found in %s", directory)
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
    parser.add_argument("--path", default=None,
                        help="指定 CSV 目录 (覆盖默认路径)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="静默模式，只显示汇总")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    engine = create_engine(DATABASE_URL, echo=False)

    @event.listens_for(engine, "connect")
    def _wal(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    total_stock = 0
    total_concept = 0

    if args.path:
        # 指定了路径，智能判断类型
        path_lower = args.path.lower()
        if args.type == "concept" or "concept" in path_lower:
            total_concept = import_concept(engine, args.path)
        else:
            total_stock = import_stock(engine, args.path)
    else:
        if args.type is None or args.type == "stock":
            total_stock = import_stock(engine)
        if args.type is None or args.type == "concept":
            total_concept = import_concept(engine)

    logger.info("=== Import done: stock=%d, concept=%d ===", total_stock, total_concept)


if __name__ == "__main__":
    main()
