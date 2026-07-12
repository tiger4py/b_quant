"""每日ETF数据采集 — 新浪数据源，按年归档: data/etf/{year}/YYYYMM.csv

用法:
    python script/update_etf.py                        # 拉取主流ETF(成交额>5亿)最近交易日
    python script/update_etf.py --date 2026-07-01      # 指定日期
    python script/update_etf.py --all                  # 全量1577只(慢，需多批)
    python script/update_etf.py --min-amount 1.0       # 成交额>1亿
    python script/update_etf.py --mode init            # 首次：拉近2年历史(主流ETF)
"""
import os, sys, csv, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import argparse
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from urllib import request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ETF_DIR = os.path.join(ROOT_DIR, "data", "etf")
MAX_BARS = 3500  # 约14年，覆盖2014至今

# ======== 分批参数 ========
BATCH_SIZE = 50          # 每批多少只（缩小批次减少被封风险）
BATCH_COOLDOWN = 180      # 批次间冷却秒数

FIELD_NAMES = ["code", "name", "trade_date", "open", "high", "low", "close", "volume", "amount"]


def _get_etf_list(all_etfs: bool = False, min_amount: float = None):
    """获取ETF列表

    参数:
        all_etfs: True=全量1577只
        min_amount: 最低成交额(亿)，None=使用缓存的主流列表
    返回: [{"code": "sh510300", "name": "沪深300ETF"}, ...]
    """
    cache_path = os.path.join(ROOT_DIR, "data", "etf_codes.json")
    main_cache_path = os.path.join(ROOT_DIR, "data", "etf_codes_main.json")

    # 全量模式
    if all_etfs:
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 86400:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        import akshare as ak
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        codes = [{"code": row["代码"], "name": row["名称"]} for _, row in df.iterrows()]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(codes, f, ensure_ascii=False)
        logger.info("ETF列表(全量): %d 只, 已缓存", len(codes))
        return codes

    # 自定义成交额阈值
    if min_amount is not None:
        import akshare as ak
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        df["成交额"] = df["成交额"].astype(float)
        df = df[df["成交额"] > min_amount * 1e8]
        codes = [{"code": row["代码"], "name": row["名称"]} for _, row in df.iterrows()]
        logger.info("ETF列表(成交额>%.1f亿): %d 只", min_amount, len(codes))
        return codes

    # 默认：加载缓存的主流列表
    if os.path.exists(main_cache_path):
        with open(main_cache_path, "r", encoding="utf-8") as f:
            codes = json.load(f)
        # 如果缓存超过7天，后台刷新
        if time.time() - os.path.getmtime(main_cache_path) > 604800:
            logger.info("主流ETF缓存超过7天，建议手动刷新: python _filter_etf.py")
        return codes

    # 没有缓存，现场生成
    logger.info("无主流ETF缓存，现场筛选...")
    import akshare as ak
    df = ak.fund_etf_category_sina(symbol="ETF基金")
    df["成交额"] = df["成交额"].astype(float)
    df = df[df["成交额"] > 5e8]  # 默认5亿
    codes = [{"code": row["代码"], "name": row["名称"]} for _, row in df.iterrows()]
    with open(main_cache_path, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False)
    logger.info("ETF列表(成交额>5亿): %d 只, 已缓存", len(codes))
    return codes


def _sina_klines(code: str, count: int = 5000):
    """从新浪拉取单只 ETF 的日线数据"""
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={count}")
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw and raw != "null" else []
    except Exception as e:
        logger.warning("新浪K线请求失败 %s: %s", code, e)
        return []


def _filter_by_date(klines, start, end):
    return [k for k in klines if start <= k["day"] <= end]


def _month_path(month_str):
    """month_str='2026-07' → data/etf/2026/2026-07.csv"""
    year = month_str[:4]
    d = os.path.join(ETF_DIR, year)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{month_str}.csv")


def _load_existing_month(month_str):
    """读取已有月度 CSV"""
    fp = _month_path(month_str)
    existing = defaultdict(dict)
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                existing[row["code"]][row["trade_date"]] = row
    return existing


def _save_months(rows_by_month):
    """按年月写入 CSV: data/etf/{year}/YYYY-MM.csv"""
    for month_str in sorted(rows_by_month):
        fp = _month_path(month_str)
        existing = _load_existing_month(month_str)
        for code, date_rows in rows_by_month[month_str].items():
            for date_str, row in date_rows.items():
                existing[code][date_str] = row

        all_rows = []
        for code in sorted(existing):
            all_rows.extend(existing[code][d] for d in sorted(existing[code]))

        if all_rows:
            with open(fp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=FIELD_NAMES)
                w.writeheader()
                w.writerows(all_rows)
            logger.info("  -> %s: %d 行(%d只ETF)", fp, len(all_rows), len(set(r["code"] for r in all_rows)))


def _process_batch(etf_batch, start_date, end_date):
    """处理一批ETF，返回 (rows_by_month, row_count, fail_count, blocked_count)"""
    rows_by_month = defaultdict(lambda: defaultdict(dict))
    row_count = 0
    fail_count = 0
    blocked_count = 0

    for etf in etf_batch:
        code, name = etf["code"], etf["name"]
        klines = _sina_klines(code)
        if len(klines) > MAX_BARS:
            klines = klines[-MAX_BARS:]
        filtered = _filter_by_date(klines, start_date, end_date)

        if not klines:
            blocked_count += 1
        else:
            for k in filtered:
                trade_date = k["day"]
                month = trade_date[:7]
                vol = int(float(k["volume"]))
                close_p = float(k["close"])
                row = {
                    "code": code, "name": name, "trade_date": trade_date,
                    "open": float(k["open"]), "high": float(k["high"]),
                    "low": float(k["low"]), "close": close_p,
                    "volume": vol,
                    "amount": round(vol * (float(k["open"]) + float(k["high"])
                                           + float(k["low"]) + close_p) / 4, 2),
                }
                rows_by_month[month][code][trade_date] = row
                row_count += 1

        time.sleep(0.3)

    return rows_by_month, row_count, fail_count, blocked_count


def download_etfs(start_date: str, end_date: str, all_etfs: bool = False, min_amount: float = None):
    """拉取 ETF 日线，分批避免限流

    参数:
        start_date, end_date: 日期范围
        all_etfs: True=全量1577只
        min_amount: 最低成交额(亿)，None=默认5亿
    """
    etf_list = _get_etf_list(all_etfs=all_etfs, min_amount=min_amount)
    total = len(etf_list)
    total_rows = 0
    total_fails = 0
    total_blocked = 0

    # 分批
    batches = [etf_list[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    logger.info("=== ETF数据采集: %s ~ %s, 共%d只, 分%d批 ===",
                start_date, end_date, total, len(batches))

    for batch_idx, batch in enumerate(batches):
        batch_start = time.time()
        rows_by_month, row_count, fail_count, blocked_count = _process_batch(
            batch, start_date, end_date
        )

        total_rows += row_count
        total_fails += fail_count
        total_blocked += blocked_count

        # 每批结束就保存
        _save_months(rows_by_month)

        pct = (batch_idx + 1) / len(batches) * 100
        logger.info("批次 %d/%d (%.0f%%): rows=%d, fails=%d, blocked=%d, 耗时%.0fs",
                    batch_idx + 1, len(batches), pct,
                    row_count, fail_count, blocked_count,
                    time.time() - batch_start)

        # 批次间冷却（最后一批不用等）
        if batch_idx < len(batches) - 1:
            blocked_ratio = blocked_count / max(1, len(batch))
            if blocked_ratio > 0.3:
                cooldown = BATCH_COOLDOWN * 2
                logger.warning("被封率 %.0f%%, 冷却加长至 %ds", blocked_ratio * 100, cooldown)
            else:
                cooldown = BATCH_COOLDOWN
            logger.info("冷却 %ds...", cooldown)
            time.sleep(cooldown)

    logger.info("完成: %d只ETF, %d行数据, %d失败, %d被封",
                total, total_rows, total_fails, total_blocked)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ETF 日线数据采集（新浪源）")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--mode", choices=["daily", "init"], default="daily")
    parser.add_argument("--start", default=None, help="init模式的起始日期")
    parser.add_argument("--end", default=None, help="init模式的结束日期")
    parser.add_argument("--all", action="store_true", help="全量1577只ETF")
    parser.add_argument("--min-amount", type=float, default=None,
                        help="最低成交额(亿)，默认5亿")
    args = parser.parse_args()

    if args.mode == "init":
        start = args.start or (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        end = args.end or datetime.now().strftime("%Y-%m-%d")
    else:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        start = end = target

    download_etfs(start, end, all_etfs=args.all, min_amount=args.min_amount)
