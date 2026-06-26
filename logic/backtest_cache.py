"""回测数据加载 & 文件归档（不再使用数据库缓存）"""
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func

from backtest import get_strategy, run_portfolio_backtest
from models.stock import StockDaily, StockInfo

DEFAULT_MARKET_INITIAL_CASH = 1000000.0
DEFAULT_MARKET_MAX_POSITIONS = 5
FIXED_START_DATE = "2022-05-06"

ROOT_DIR = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = ROOT_DIR / "data" / "strategy"


# ============ 文件读取 ============

def load_latest_strategy_result(strategy_id):
    """从 archive 文件中读取策略的最新回测结果。"""
    strategy_dir = ARCHIVE_ROOT / strategy_id
    if not strategy_dir.exists():
        return None

    # 按月份降序排列
    month_dirs = sorted(
        [d for d in strategy_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name, reverse=True,
    )
    for month_dir in month_dirs:
        files = sorted(month_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            with open(files[0], "r", encoding="utf-8") as f:
                result = json.load(f)
            mtime = files[0].stat().st_mtime
            result["cache"] = {
                "cache_key": f"{strategy_id}_file",
                "created_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            return result
    return None


# ============ 数据加载 ============

def load_market_bars(sess, start_date=None, end_date=None):
    """加载全市场K线数据。"""
    latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
    if not latest_date:
        return [], {}, latest_date

    effective_start = start_date or FIXED_START_DATE
    effective_end = end_date or latest_date
    effective_latest = effective_end

    # 股票列表：在 end_date 当天活跃的股票
    latest_rows = (
        sess.query(StockInfo, StockDaily)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date == effective_latest,
        )
        .all()
    )
    stock_map = {
        stock.code: {
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "latest_trade_date": effective_latest,
            "latest_amount": daily.amount or 0,
        }
        for stock, daily in latest_rows
    }

    bars_by_code = {code: [] for code in stock_map}
    rows = (
        sess.query(StockDaily)
        .join(StockInfo, StockDaily.code == StockInfo.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date >= effective_start,
            StockDaily.trade_date <= effective_end,
        )
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )
    for row in rows:
        if row.code in bars_by_code:
            bars_by_code[row.code].append(daily_to_dict(row))

    min_count = 120
    stocks = []
    clean_bars = {}
    for code, bars in bars_by_code.items():
        if len(bars) < min_count:
            continue
        item = stock_map[code]
        item["daily_count"] = len(bars)
        stocks.append(item)
        clean_bars[code] = bars

    stocks.sort(key=lambda x: x["code"])
    return stocks, clean_bars, latest_date


def daily_to_dict(row):
    return {
        "trade_date": row.trade_date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
        "amount": row.amount,
        "turn": getattr(row, "turn", None),
        "pe_ttm": getattr(row, "pe_ttm", None),
    }


# ============ 回测计算 ============

def compute_market_result(
    sess,
    strategy_id,
    start_date=None,
    end_date=None,
    initial_cash=DEFAULT_MARKET_INITIAL_CASH,
    max_positions=DEFAULT_MARKET_MAX_POSITIONS,
    criteria="全部正常股票，剔除历史K线不足的股票",
):
    strategy = get_strategy(strategy_id)
    stocks, bars_by_code, latest_date = load_market_bars(sess, start_date=start_date, end_date=end_date)
    if not stocks:
        raise ValueError("没有找到可用于全市场回测的股票")

    stock_map = {stock["code"]: stock for stock in stocks}
    result = run_portfolio_backtest(
        bars_by_code, stock_map, strategy,
        initial_cash=initial_cash, max_positions=max_positions,
    )
    trades = result["trades"]
    trades.sort(key=lambda x: (x["buy_date"], x["code"]))
    return {
        "strategy": strategy.META,
        "selection": {
            "stock_count": len(stocks),
            "start_date": start_date or FIXED_START_DATE,
            "end_date": end_date or latest_date,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
            "latest_trade_date": latest_date,
            "criteria": criteria,
            "cached": True,
        },
        "summary": result["summary"],
        "equity_curve": result["equity_curve"],
        "stock_summaries": result["stock_summaries"],
        "trades": trades,
        "market_gate": result.get("market_gate"),
    }


def make_market_cache_key(strategy_id, start_date=None, end_date=None, max_positions=DEFAULT_MARKET_MAX_POSITIONS):
    s = start_date or FIXED_START_DATE
    e = end_date or "latest"
    return f"{strategy_id}_market_{s}_{e}_pos{int(max_positions)}"


def _save_strategy_archive(strategy_id, result):
    """将回测结果归档到 data/strategy/{策略}/{年}-{月}/{日期}_{序号}.json"""
    latest_date = result.get("selection", {}).get("latest_trade_date", "")
    year_month = latest_date[:7] if len(latest_date) >= 7 else datetime.now().strftime("%Y-%m")

    archive_dir = ARCHIVE_ROOT / strategy_id / year_month
    archive_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"{latest_date}_"
    max_seq = 0
    for f in archive_dir.glob(f"{prefix}*.json"):
        try:
            seq = int(f.stem[len(prefix):])
            if seq > max_seq:
                max_seq = seq
        except ValueError:
            pass

    archive_path = archive_dir / f"{prefix}{max_seq + 1:02d}.json"
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[archive] {strategy_id} -> {archive_path}")


def compute_and_save_market_result(
    sess,
    strategy_id,
    start_date=None,
    end_date=None,
    initial_cash=DEFAULT_MARKET_INITIAL_CASH,
    max_positions=DEFAULT_MARKET_MAX_POSITIONS,
    criteria="全部正常股票，剔除历史K线不足的股票",
):
    """跑回测 + 存文件。不再写数据库。"""
    result = compute_market_result(
        sess, strategy_id=strategy_id,
        start_date=start_date, end_date=end_date,
        initial_cash=initial_cash, max_positions=max_positions,
        criteria=criteria,
    )
    cache_key = make_market_cache_key(strategy_id, start_date=start_date, end_date=end_date, max_positions=max_positions)
    _save_strategy_archive(strategy_id, result)
    return cache_key, result
