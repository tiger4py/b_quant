import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template, request, url_for
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS
from script.update_daily import update_concepts as scheduled_update_concepts
from script.update_daily import update_stocks as scheduled_update_stocks
from backtest import get_strategy, list_strategies, run_backtest, run_portfolio_backtest
from logic.backtest_cache import (
    ACCUMULATION_MARKET_CACHE_KEY,
    DEFAULT_MARKET_DAYS,
    DEFAULT_MARKET_MAX_POSITIONS,
    MACD_MARKET_CACHE_KEY,
    load_backtest_cache,
    load_market_backtest_cache,
)
from logic.progress import get as get_progress
from models.stock import Base, StockInfo, StockDaily, Concept, StockConcept, ConceptDaily
from logic.baostock_download import BaoStockDownloader
from logic.akshare_download import AkShareDownloader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)
_scheduler_thread = None
_scheduler_lock = threading.Lock()
_last_scheduler_run_date = None


def get_session() -> Session:
    return SessionLocal()


Base.metadata.create_all(engine)


def _run_daily_update_job():
    logger.info("scheduled daily update started")
    try:
        scheduled_update_stocks()
    except Exception:
        logger.exception("scheduled stock update failed")

    try:
        scheduled_update_concepts()
    except Exception:
        logger.exception("scheduled concept update failed")
    logger.info("scheduled daily update finished")


def _scheduler_loop():
    global _last_scheduler_run_date
    logger.info("flask scheduler started: workdays 18:00 update_daily")

    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        should_run = (
            now.weekday() < 5
            and now.hour == 18
            and now.minute == 2
            and _last_scheduler_run_date != today
        )

        if should_run:
            _last_scheduler_run_date = today
            _run_daily_update_job()

        time.sleep(30)


def start_scheduler():
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            name="daily-update-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/trades")
def page_trades():
    return render_template("trades.html")


@app.route("/api/trades")
def api_trades():
    """返回交易历史记录。"""
    import json
    from pathlib import Path
    history_file = Path(__file__).resolve().parent / "data" / "trade_history.json"
    if not history_file.exists():
        return jsonify({"records": [], "summary": {}})
    with open(history_file, "r", encoding="utf-8") as f:
        records = json.load(f)

    # 汇总统计
    total_sells = sum(len(r.get("sells", [])) for r in records)
    total_value = records[-1]["total_value"] if records else 0
    first_value = records[0]["total_value"] if records else 0
    total_return = (total_value / first_value - 1) * 100 if first_value > 0 else 0
    total_return = round(total_return, 2)

    # 按日期倒序
    records.reverse()

    return jsonify({
        "records": records,
        "summary": {
            "days": len(records),
            "total_sells": total_sells,
            "initial_value": first_value,
            "current_value": total_value,
            "total_return_pct": total_return,
        },
    })


@app.route("/stocks")
def page_stocks():
    return render_template("stocks.html")


@app.route("/concepts")
def page_concepts():
    return render_template("concepts.html")


@app.route("/strategy-backtest")
def page_strategy_backtest():
    return render_template("strategy_backtest.html")


@app.route("/stock-backtest")
def page_stock_backtest():
    return redirect(url_for("page_strategy_backtest"))


@app.route("/backtest-battle")
def page_backtest_battle():
    return redirect(url_for("page_strategy_backtest"))


@app.route("/macd-market-backtest")
def page_macd_market_backtest():
    return redirect(url_for("page_strategy_backtest"))


@app.route("/accumulation-market-backtest")
def page_accumulation_market_backtest():
    return redirect(url_for("page_strategy_backtest"))


# ── stock basic info ───────────────────────────────────────────
@app.route("/api/stocks")
def api_stocks():
    q = request.args.get("q", "")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 30))
    with get_session() as sess:
        base = sess.query(StockInfo).filter(StockInfo.type == "1", StockInfo.status == 1)
        if q:
            base = base.filter(
                StockInfo.code.contains(q) | StockInfo.name.contains(q)
            )
        total = base.count()
        rows = base.order_by(StockInfo.code).offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [{
                "code": s.code, "name": s.name, "market": s.market,
                "ipo_date": s.ipo_date,
            } for s in rows],
        })


# ── daily K-line ───────────────────────────────────────────────
@app.route("/api/stocks/<code>/daily")
def api_stock_daily(code: str):
    with get_session() as sess:
        rows = (
            sess.query(StockDaily)
            .filter(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(365)
            .all()
        )
        return jsonify([{
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
            "turn": getattr(r, "turn", None), "pe_ttm": getattr(r, "pe_ttm", None),
        } for r in reversed(rows)])


@app.route("/api/stocks/prices")
def api_stocks_prices():
    """批量获取股票在指定日期的收盘价。codes=逗号分隔, date=YYYY-MM-DD"""
    codes_str = request.args.get("codes", "")
    date = request.args.get("date", "")
    if not codes_str or not date:
        return jsonify({})
    codes = [c.strip() for c in codes_str.split(",") if c.strip()]
    if not codes:
        return jsonify({})
    with get_session() as sess:
        rows = (
            sess.query(StockDaily.code, StockDaily.close)
            .filter(StockDaily.code.in_(codes), StockDaily.trade_date == date)
            .all()
        )
    return jsonify({row.code: row.close for row in rows})


@app.route("/api/backtest/strategies")
def api_backtest_strategies():
    return jsonify([{
        **item,
        "market_cache_key": f"{item['id']}_market_{DEFAULT_MARKET_DAYS}_pos{DEFAULT_MARKET_MAX_POSITIONS}",
        "market_cache_days": DEFAULT_MARKET_DAYS,
        "market_cache_max_positions": DEFAULT_MARKET_MAX_POSITIONS,
    } for item in list_strategies()])


@app.route("/api/backtest/market-overview")
def api_backtest_market_overview():
    days = max(120, min(int(request.args.get("days") or DEFAULT_MARKET_DAYS), 2000))
    max_positions = max(1, min(int(request.args.get("max_positions") or DEFAULT_MARKET_MAX_POSITIONS), 5))
    ranking = []
    missing = []
    with get_session() as sess:
        for item in list_strategies():
            result = load_market_backtest_cache(
                sess,
                strategy_id=item["id"],
                days=days,
                max_positions=max_positions,
            )
            if not result:
                missing.append({
                    "strategy_id": item["id"],
                    "strategy_name": item["name"],
                })
                continue
            summary = result["summary"]
            ranking.append({
                "strategy_id": item["id"],
                "strategy_name": item["name"],
                "description": item["description"],
                "return_pct": summary["total_return_pct"],
                "drawdown_pct": summary["max_drawdown_pct"],
                "win_rate_pct": summary["win_rate_pct"],
                "trade_count": summary["trade_count"],
                "final_equity": summary["final_equity"],
                "score": round(summary["total_return_pct"] - summary["max_drawdown_pct"] * 0.35 + summary["win_rate_pct"] * 0.05, 2),
                "latest_trade_date": result["selection"]["latest_trade_date"],
                "stock_count": result["selection"]["stock_count"],
                "cache_created_at": result.get("cache", {}).get("created_at"),
            })
    ranking.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({
        "days": days,
        "max_positions": max_positions,
        "ranking": ranking,
        "missing": missing,
    })


@app.route("/api/backtest/stock", methods=["POST"])
def api_backtest_stock():
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    if len(code) == 6 and code.isdigit():
        code = f"sh.{code}" if code.startswith("6") else f"sz.{code}"
    strategy_id = (payload.get("strategy") or "").strip()
    initial_cash = float(payload.get("initial_cash") or 100000)
    days = int(payload.get("days") or 1000)

    if not code:
        return jsonify({"error": "请输入股票代码"}), 400
    if not strategy_id:
        return jsonify({"error": "请选择策略"}), 400

    try:
        strategy = get_strategy(strategy_id)
    except KeyError:
        return jsonify({"error": "策略不存在"}), 404

    with get_session() as sess:
        stock = sess.get(StockInfo, code)
        rows = (
            sess.query(StockDaily)
            .filter(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(days)
            .all()
        )

    bars = [{
        "trade_date": r.trade_date,
        "open": r.open,
        "high": r.high,
        "low": r.low,
        "close": r.close,
        "volume": r.volume,
        "amount": r.amount,
        "turn": getattr(r, "turn", None),
        "pe_ttm": getattr(r, "pe_ttm", None),
    } for r in reversed(rows)]

    try:
        result = run_backtest(bars, strategy, initial_cash=initial_cash)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    result["stock"] = {
        "code": code,
        "name": stock.name if stock else code,
    }
    result["bars"] = bars
    return jsonify(result)


@app.route("/api/backtest/battle", methods=["POST"])
def api_backtest_battle():
    payload = request.get_json(silent=True) or {}
    stock_limit = max(5, min(int(payload.get("stock_limit") or 50), 300))
    days = max(60, min(int(payload.get("days") or 500), 1500))
    initial_cash = float(payload.get("initial_cash") or 1000000)
    max_positions = 5

    strategies = [get_strategy(item["id"]) for item in list_strategies()]
    with get_session() as sess:
        stocks = _pick_battle_stocks(sess, stock_limit, days)
        if not stocks:
            return jsonify({"error": "没有找到可用于回测的股票"}), 400

        bars_by_code = {}
        for stock in stocks:
            rows = (
                sess.query(StockDaily)
                .filter(StockDaily.code == stock["code"])
                .order_by(StockDaily.trade_date.desc())
                .limit(days)
                .all()
            )
            bars_by_code[stock["code"]] = [_daily_to_dict(r) for r in reversed(rows)]

    stock_map = {stock["code"]: stock for stock in stocks}
    ranking = []
    details = []
    for strategy in strategies:
        try:
            result = run_portfolio_backtest(
                bars_by_code,
                stock_map,
                strategy,
                initial_cash=initial_cash,
                max_positions=max_positions,
            )
        except ValueError:
            continue

        summary = result["summary"]
        stock_results = [{
            "strategy_id": strategy.META["id"],
            "strategy_name": strategy.META["name"],
            "code": item["code"],
            "name": item["name"],
            "profit": item["profit"],
            "trade_count": item["trade_count"],
            "win_rate_pct": item["win_rate_pct"],
        } for item in result["stock_summaries"]]
        details.extend(stock_results)

        best_stock = max(stock_results, key=lambda x: x["profit"]) if stock_results else None
        worst_stock = min(stock_results, key=lambda x: x["profit"]) if stock_results else None
        ranking.append({
            "strategy_id": strategy.META["id"],
            "strategy_name": strategy.META["name"],
            "description": strategy.META["description"],
            "stock_count": len(stocks),
            "avg_return_pct": summary["total_return_pct"],
            "avg_drawdown_pct": summary["max_drawdown_pct"],
            "avg_win_rate_pct": summary["win_rate_pct"],
            "avg_trade_count": summary["trade_count"],
            "positive_count": len([x for x in result["trades"] if x["profit"] > 0]),
            "positive_rate_pct": summary["win_rate_pct"],
            "score": round(summary["total_return_pct"] - summary["max_drawdown_pct"] * 0.35 + summary["win_rate_pct"] * 0.05, 2),
            "final_equity": summary["final_equity"],
            "trade_count": summary["trade_count"],
            "best_stock": best_stock,
            "worst_stock": worst_stock,
        })

    ranking.sort(key=lambda x: x["score"], reverse=True)
    details.sort(key=lambda x: (x["strategy_name"], -x["profit"]))
    return jsonify({
        "selection": {
            "stock_count": len(stocks),
            "days": days,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
            "criteria": "最新交易日成交额靠前，且历史K线数量满足回测天数",
            "latest_trade_date": stocks[0]["latest_trade_date"] if stocks else None,
        },
        "stocks": stocks,
        "ranking": ranking,
        "details": details,
    })


@app.route("/api/backtest/battle/strategy", methods=["POST"])
def api_backtest_battle_strategy():
    payload = request.get_json(silent=True) or {}
    strategy_id = (payload.get("strategy") or "").strip()
    stock_limit = max(5, min(int(payload.get("stock_limit") or 50), 300))
    days = max(60, min(int(payload.get("days") or 500), 1500))
    initial_cash = float(payload.get("initial_cash") or 1000000)
    max_positions = 5

    if not strategy_id:
        return jsonify({"error": "请选择策略"}), 400

    try:
        strategy = get_strategy(strategy_id)
    except KeyError:
        return jsonify({"error": "策略不存在"}), 404

    with get_session() as sess:
        stocks = _pick_battle_stocks(sess, stock_limit, days)
        stock_map = {s["code"]: s for s in stocks}
        rows_by_code = {}
        for stock in stocks:
            rows = (
                sess.query(StockDaily)
                .filter(StockDaily.code == stock["code"])
                .order_by(StockDaily.trade_date.desc())
                .limit(days)
                .all()
            )
            rows_by_code[stock["code"]] = [_daily_to_dict(r) for r in reversed(rows)]

    try:
        result = run_portfolio_backtest(
            rows_by_code,
            stock_map,
            strategy,
            initial_cash=initial_cash,
            max_positions=max_positions,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    trades = result["trades"]
    trades.sort(key=lambda x: (x["buy_date"], x["code"]))
    return jsonify({
        "strategy": strategy.META,
        "selection": {
            "stock_count": len(stocks),
            "days": days,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
        },
        "summary": result["summary"],
        "equity_curve": result["equity_curve"],
        "stock_summaries": result["stock_summaries"],
        "trades": trades,
    })


@app.route("/api/backtest/stability", methods=["POST"])
def api_backtest_stability():
    payload = request.get_json(silent=True) or {}
    initial_cash = float(payload.get("initial_cash") or 1000000)
    max_positions = 5
    stock_limits = [30, 50, 100]
    day_windows = [120, 250, 500]
    strategies = [get_strategy(item["id"]) for item in list_strategies()]

    stats = {
        strategy.META["id"]: {
            "strategy_id": strategy.META["id"],
            "strategy_name": strategy.META["name"],
            "runs": 0,
            "rank_sum": 0,
            "top3_count": 0,
            "champion_count": 0,
            "return_sum": 0,
            "worst_return_pct": None,
            "drawdown_sum": 0,
            "win_rate_sum": 0,
        }
        for strategy in strategies
    }
    runs = []

    with get_session() as sess:
        for days in day_windows:
            stocks_all = _pick_battle_stocks(sess, max(stock_limits), days)
            if not stocks_all:
                continue

            bars_all = {}
            for stock in stocks_all:
                rows = (
                    sess.query(StockDaily)
                    .filter(StockDaily.code == stock["code"])
                    .order_by(StockDaily.trade_date.desc())
                    .limit(days)
                    .all()
                )
                bars_all[stock["code"]] = [_daily_to_dict(r) for r in reversed(rows)]

            for stock_limit in stock_limits:
                stocks = stocks_all[:stock_limit]
                stock_map = {stock["code"]: stock for stock in stocks}
                bars_by_code = {stock["code"]: bars_all[stock["code"]] for stock in stocks}
                run_results = []
                for strategy in strategies:
                    try:
                        result = run_portfolio_backtest(
                            bars_by_code,
                            stock_map,
                            strategy,
                            initial_cash=initial_cash,
                            max_positions=max_positions,
                        )
                    except ValueError:
                        continue
                    summary = result["summary"]
                    run_results.append({
                        "strategy_id": strategy.META["id"],
                        "strategy_name": strategy.META["name"],
                        "return_pct": summary["total_return_pct"],
                        "drawdown_pct": summary["max_drawdown_pct"],
                        "win_rate_pct": summary["win_rate_pct"],
                    })

                run_results.sort(key=lambda x: x["return_pct"], reverse=True)
                for rank, item in enumerate(run_results, start=1):
                    row = stats[item["strategy_id"]]
                    row["runs"] += 1
                    row["rank_sum"] += rank
                    row["top3_count"] += 1 if rank <= 3 else 0
                    row["champion_count"] += 1 if rank == 1 else 0
                    row["return_sum"] += item["return_pct"]
                    row["drawdown_sum"] += item["drawdown_pct"]
                    row["win_rate_sum"] += item["win_rate_pct"]
                    row["worst_return_pct"] = (
                        item["return_pct"]
                        if row["worst_return_pct"] is None
                        else min(row["worst_return_pct"], item["return_pct"])
                    )

                runs.append({
                    "days": days,
                    "stock_limit": stock_limit,
                    "ranking": run_results,
                })

    ranking = []
    for row in stats.values():
        if not row["runs"]:
            continue
        avg_rank = row["rank_sum"] / row["runs"]
        avg_return = row["return_sum"] / row["runs"]
        avg_drawdown = row["drawdown_sum"] / row["runs"]
        avg_win_rate = row["win_rate_sum"] / row["runs"]
        ranking.append({
            "strategy_id": row["strategy_id"],
            "strategy_name": row["strategy_name"],
            "runs": row["runs"],
            "avg_rank": round(avg_rank, 2),
            "top3_count": row["top3_count"],
            "champion_count": row["champion_count"],
            "avg_return_pct": round(avg_return, 2),
            "worst_return_pct": round(row["worst_return_pct"], 2),
            "avg_drawdown_pct": round(avg_drawdown, 2),
            "avg_win_rate_pct": round(avg_win_rate, 2),
            "score": round((10 - avg_rank) * 10 + avg_return - avg_drawdown * 0.3, 2),
        })

    ranking.sort(key=lambda x: (x["avg_rank"], -x["avg_return_pct"]))
    return jsonify({
        "settings": {
            "stock_limits": stock_limits,
            "day_windows": day_windows,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "run_count": len(runs),
        },
        "ranking": ranking,
        "runs": runs,
    })


@app.route("/api/backtest/macd-market")
def api_backtest_macd_market():
    with get_session() as sess:
        result = load_backtest_cache(sess, MACD_MARKET_CACHE_KEY)
    if not result:
        return jsonify({
            "error": "MACD 全市场 1000 天回测结果还没有生成，请先运行 script/run_macd_market_backtest.py"
        }), 404
    return jsonify(result)


@app.route("/api/backtest/accumulation-market")
def api_backtest_accumulation_market():
    with get_session() as sess:
        result = load_backtest_cache(sess, ACCUMULATION_MARKET_CACHE_KEY)
    if not result:
        return jsonify({
            "error": "吸筹试盘全市场1000天回测结果还没有生成，请先运行 script/run_accumulation_market_backtest.py"
        }), 404
    return jsonify(result)


@app.route("/api/backtest/market/<strategy_id>")
def api_backtest_market(strategy_id: str):
    days = max(120, min(int(request.args.get("days") or DEFAULT_MARKET_DAYS), 2000))
    max_positions = max(1, min(int(request.args.get("max_positions") or DEFAULT_MARKET_MAX_POSITIONS), 5))
    try:
        strategy = get_strategy(strategy_id)
    except KeyError:
        return jsonify({"error": "策略不存在"}), 404

    with get_session() as sess:
        result = load_market_backtest_cache(
            sess,
            strategy_id=strategy_id,
            days=days,
            max_positions=max_positions,
        )
    if not result:
        return jsonify({
            "error": f"{strategy.META['name']} 全市场 {days} 天回测结果还没有生成，请先运行 script/run_strategy_market_backtest.py --strategy {strategy_id} --days {days} --max-positions {max_positions}"
        }), 404
    return jsonify(result)


def _pick_battle_stocks(sess: Session, stock_limit: int, days: int):
    latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
    candidates = (
        sess.query(StockInfo, StockDaily)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date == latest_date,
            StockDaily.amount.isnot(None),
        )
        .order_by(StockDaily.amount.desc())
        .limit(stock_limit * 5)
        .all()
    )

    picked = []
    for stock, daily in candidates:
        count = sess.query(func.count(StockDaily.id)).filter(StockDaily.code == stock.code).scalar()
        if count < min(days, 120):
            continue
        picked.append({
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "latest_trade_date": latest_date,
            "latest_amount": daily.amount,
            "daily_count": count,
        })
        if len(picked) >= stock_limit:
            break
    return picked


def _load_market_bars(sess: Session, days: int):
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
            bars_by_code[row.code].append(_daily_to_dict(row))

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


def _daily_to_dict(row: StockDaily):
    return {
        "trade_date": row.trade_date,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
        "amount": row.amount,
        # "turn": row.turn,
        "pe_ttm": row.pe_ttm,
    }


# ── concepts ───────────────────────────────────────────────────
@app.route("/api/concepts")
def api_concepts():
    with get_session() as sess:
        concepts = sess.query(Concept).all()
        return jsonify([{"code": c.code, "name": c.name} for c in concepts])


@app.route("/api/concepts/<concept_code>/daily")
def api_concept_daily(concept_code: str):
    with get_session() as sess:
        rows = (
            sess.query(ConceptDaily)
            .filter(ConceptDaily.concept_code == concept_code)
            .order_by(ConceptDaily.trade_date.desc())
            .limit(1000)
            .all()
        )
        return jsonify([{
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
        } for r in reversed(rows)])


@app.route("/api/concepts/<concept_code>/stocks")
def api_concept_stocks(concept_code: str):
    with get_session() as sess:
        concept = sess.get(Concept, concept_code)
        if not concept:
            return jsonify({"error": "concept not found"}), 404

        rows = (
            sess.query(StockConcept, StockInfo)
            .join(StockInfo, StockConcept.stock_code == StockInfo.code)
            .filter(StockConcept.concept_code == concept_code)
            .all()
        )
        return jsonify({
            "concept": {"code": concept.code, "name": concept.name},
            "stocks": [{
                "code": si.code, "name": si.name,
            } for _, si in rows],
        })


# ── stats ──────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    with get_session() as sess:
        stock_count = sess.query(func.count(StockInfo.code)).filter(StockInfo.type == "1", StockInfo.status == 1).scalar()
        daily_count = sess.query(func.count(StockDaily.id)).scalar()
        concept_count = sess.query(func.count(Concept.code)).scalar()
        rel_count = sess.query(func.count(StockConcept.id)).scalar()
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
        return jsonify({
            "stocks": stock_count,
            "daily_records": daily_count,
            "concepts": concept_count,
            "stock_concept_relations": rel_count,
            "latest_trade_date": latest_date,
        })


# ── download progress ──────────────────────────────────────────
@app.route("/api/download/progress")
def api_progress():
    return jsonify(get_progress())


# ── download triggers ──────────────────────────────────────────
@app.route("/api/download/stocks", methods=["POST"])
def download_stocks():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    with get_session() as sess:
        dl = BaoStockDownloader(sess, days)
        n = dl.download_stock_basic()
        return jsonify({"status": "ok", "new_stocks": n})


@app.route("/api/download/daily", methods=["POST"])
def download_daily():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    code = request.json.get("code") if request.is_json else None
    with get_session() as sess:
        dl = BaoStockDownloader(sess, days)
        n = dl.download_daily_k(code=code)
        return jsonify({"status": "ok", "rows_upserted": n})


@app.route("/api/download/concepts", methods=["POST"])
def download_concepts():
    with get_session() as sess:
        dl = AkShareDownloader(sess)
        c, r = dl.download_concepts()
        return jsonify({"status": "ok", "new_concepts": c, "new_relations": r})


@app.route("/api/download/concept_daily", methods=["POST"])
def download_concept_daily():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    with get_session() as sess:
        dl = AkShareDownloader(sess)
        n = dl.download_concept_daily(days=days)
        return jsonify({"status": "ok", "rows": n})


if __name__ == "__main__":
    debug = True
    # Flask debug reload starts the app twice; only start the scheduler in the serving process.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    app.run(host="0.0.0.0", port=8000, debug=False)
