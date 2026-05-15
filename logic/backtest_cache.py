import json
from datetime import datetime

from sqlalchemy import desc, func

from backtest import get_strategy, run_portfolio_backtest
from models.stock import BacktestCache, StockDaily, StockInfo

MACD_MARKET_CACHE_KEY = "macd_market_all_1000"


def compute_macd_market_result(sess, days=1000, initial_cash=1000000.0, max_positions=5):
    strategy = get_strategy("macd_cross")
    stocks, bars_by_code, latest_date = load_market_bars(sess, days)
    if not stocks:
        raise ValueError("没有找到可用于全市场回测的股票")

    stock_map = {stock["code"]: stock for stock in stocks}
    result = run_portfolio_backtest(
        bars_by_code,
        stock_map,
        strategy,
        initial_cash=initial_cash,
        max_positions=max_positions,
    )
    trades = result["trades"]
    trades.sort(key=lambda x: (x["buy_date"], x["code"]))
    return {
        "strategy": strategy.META,
        "selection": {
            "stock_count": len(stocks),
            "days": days,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
            "latest_trade_date": latest_date,
            "criteria": "全部正常股票，剔除历史K线不足的股票",
            "cached": True,
        },
        "summary": result["summary"],
        "equity_curve": result["equity_curve"],
        "stock_summaries": result["stock_summaries"],
        "trades": trades,
    }


def save_backtest_cache(sess, cache_key, result):
    row = sess.get(BacktestCache, cache_key)
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not row:
        row = BacktestCache(cache_key=cache_key)
        sess.add(row)

    row.strategy_id = result["strategy"]["id"]
    row.name = result["strategy"]["name"]
    row.days = result["selection"]["days"]
    row.stock_count = result["selection"]["stock_count"]
    row.latest_trade_date = result["selection"]["latest_trade_date"]
    row.created_at = now
    row.result_json = payload
    sess.commit()
    return row


def load_backtest_cache(sess, cache_key):
    row = sess.get(BacktestCache, cache_key)
    if not row:
        return None
    result = json.loads(row.result_json)
    result["cache"] = {
        "cache_key": row.cache_key,
        "created_at": row.created_at,
    }
    return result


def load_market_bars(sess, days):
    latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
    date_rows = (
        sess.query(StockDaily.trade_date)
        .distinct()
        .order_by(desc(StockDaily.trade_date))
        .limit(days)
        .all()
    )
    if not date_rows:
        return [], {}, latest_date

    cutoff = min(row[0] for row in date_rows)
    latest_rows = (
        sess.query(StockInfo, StockDaily)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date == latest_date,
        )
        .all()
    )
    stock_map = {
        stock.code: {
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "latest_trade_date": latest_date,
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
            StockDaily.trade_date >= cutoff,
        )
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )
    for row in rows:
        if row.code in bars_by_code:
            bars_by_code[row.code].append(daily_to_dict(row))

    min_count = min(days, 120)
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
