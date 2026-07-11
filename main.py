import json
import logging
import os
import hashlib
import threading
import time
from datetime import datetime, date

from flask import Flask, jsonify, redirect, render_template, request, url_for
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS
from script.update_daily import update_concepts as scheduled_update_concepts
from script.update_daily import update_stocks as scheduled_update_stocks
from pathlib import Path
from backtest import get_strategy, list_strategies, run_backtest, run_portfolio_backtest
from logic.backtest_cache import (
    DEFAULT_MARKET_MAX_POSITIONS,
    load_latest_strategy_result,
)

ROOT_DIR = Path(__file__).resolve().parent
ETF_STRATEGY_ROOT = ROOT_DIR / "data" / "strategy"
from logic.progress import get as get_progress
from models.stock import Base, StockInfo, StockDaily, Concept, StockConcept, ConceptDaily
from logic.baostock_download import BaoStockDownloader
from logic.akshare_download import AkShareDownloader
from logic.gtja_alpha191 import GTJA_ALPHA_PHASE1_ACTIVE, precompute_gtja_factor_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


# ======== ETF 策略结果加载 ========

def _load_etf_strategy_result(strategy_id: str):
    """从 data/strategy/{id}/ 读取最新 ETF 回测 JSON 归档。"""
    strategy_dir = ETF_STRATEGY_ROOT / strategy_id
    if not strategy_dir.exists():
        return None

    # 按月份目录降序找最新文件
    latest_file = None
    latest_mtime = 0
    for json_file in strategy_dir.rglob("*.json"):
        mtime = json_file.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_file = json_file

    if not latest_file:
        return None

    with open(latest_file, "r", encoding="utf-8") as f:
        result = json.load(f)

    # 补充 cache 元数据
    result["cache"] = {
        "cache_key": f"{strategy_id}_etf",
        "created_at": datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 补充 days 字段
    if result.get("equity_curve"):
        result["selection"]["days"] = len(result["equity_curve"])
    return result

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

    # CSV 导入数据库
    import subprocess
    try:
        subprocess.run(
            [sys.executable, "script/import_day_stock.py", "-q"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=600,
        )
    except Exception:
        logger.exception("scheduled csv import failed")

    logger.info("scheduled daily update finished")


def _run_push_job():
    """收盘后：更新数据 → 回测 → 推送，一条龙"""
    import subprocess
    logger.info("scheduled full flow started")
    try:
        result = subprocess.run(
            ["python", "script/daily_full_flow.py", "--days", "1000", "--max-positions", "5"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            logger.error(f"full flow failed: {result.stderr[:500]}")
        else:
            logger.info("full flow finished")
    except Exception:
        logger.exception("scheduled full flow failed")


def _scheduler_loop():
    global _last_scheduler_run_date
    logger.info("flask scheduler started: workdays 18:02 update → push")

    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        is_workday = now.weekday() < 5

        # 收盘后 19:30 — 更新数据 → 回测 → QQ推送
        if is_workday and now.hour == 19 and now.minute == 30 and _last_scheduler_run_date != today:
            _last_scheduler_run_date = today
            _run_daily_update_job()
            _run_push_job()

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


# ── 实盘跟随 ──────────────────────────────────────────────────

TRADING_PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "data", "portfolio.json")
TRADING_LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "trade_log.json")
TRADING_JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "data", "trading_journal.json")


def _load_trading_portfolio():
    if not os.path.exists(TRADING_PORTFOLIO_FILE):
        return {"cash": 400000, "max_positions": 5, "holdings": []}
    with open(TRADING_PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_trading_portfolio(p):
    p.pop("active_holdings", None)
    with open(TRADING_PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def _load_trade_log():
    if not os.path.exists(TRADING_LOG_FILE):
        return []
    with open(TRADING_LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_trade_log(logs):
    with open(TRADING_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


# ── 操作心得 / 交易日志 ──

def _load_journal():
    if not os.path.exists(TRADING_JOURNAL_FILE):
        return []
    with open(TRADING_JOURNAL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_journal(entries):
    with open(TRADING_JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


@app.route("/trading")
def page_trading():
    return render_template("trading.html")


@app.route("/api/trading/state")
def api_trading_state():
    portfolio = _load_trading_portfolio()
    logs = _load_trade_log()

    with get_session() as sess:
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()

        # 查持仓现价 + 平仓线
        from backtest.indicators import sma as _sma
        from backtest.strategy.strategy_vegas_tunnel import STOP_LOSS_PCT as _SL, HIGH_RETREAT_PCT as _HR
        _VC = 0.7  # 量能萎缩比例（5日均量/20日均量 低于此值触发警报）

        holdings_with_price = []
        holding_value = 0
        for h in portfolio.get("holdings", []):
            code = h["code"]
            row = (
                sess.query(StockDaily.close, StockDaily.trade_date)
                .filter(StockDaily.code == code, StockDaily.trade_date == latest_date)
                .first()
            )
            current_price = row.close if row else h.get("buy_price", 0)
            shares = h.get("shares", 0)
            mv = current_price * shares
            pnl = mv - h["buy_price"] * shares
            pnl_pct = (current_price / h["buy_price"] - 1) * 100 if h["buy_price"] > 0 else 0

            # 持仓天数
            buy_date = datetime.strptime(h["buy_date"], "%Y-%m-%d")
            latest_dt = datetime.strptime(latest_date, "%Y-%m-%d") if latest_date else datetime.now()
            hold_days = (latest_dt - buy_date).days

            # 名称
            stock_info = sess.get(StockInfo, code)
            name = stock_info.name if stock_info else h.get("name", code)

            # ── 平仓线计算 ──
            k_rows = (
                sess.query(StockDaily.trade_date, StockDaily.close, StockDaily.high, StockDaily.low, StockDaily.volume)
                .filter(StockDaily.code == code)
                .order_by(StockDaily.trade_date.desc())
                .limit(45)
                .all()
            )
            k_rows.reverse()
            alerts = {}
            if len(k_rows) >= 20:
                closes_k = [r.close for r in k_rows]
                highs_k = [r.high for r in k_rows]
                volumes_k = [r.volume or 0 for r in k_rows]
                nk = len(closes_k)
                ik = nk - 1
                ma10_k = _sma(closes_k, 10)
                ma20_k = _sma(closes_k, 20)
                vol_ma5_k = _sma(volumes_k, 5)
                vol_ma20_k = _sma(volumes_k, 20)
                vr_k = vol_ma5_k[ik] / vol_ma20_k[ik] if vol_ma20_k[ik] > 0 else 0

                # 高点（从买入日算起）
                buy_idx_k = None
                for jk in range(nk):
                    if k_rows[jk].trade_date >= h["buy_date"]:
                        buy_idx_k = jk
                        break
                if buy_idx_k is None:
                    buy_idx_k = max(0, nk - 5)
                peak_since = max(highs_k[buy_idx_k:ik + 1])

                stop_loss_price = round(h["buy_price"] * (1 + _SL / 100), 2)
                retreat_price = round(peak_since * (1 + _HR / 100), 2)
                vol_collapse_5d = round(vol_ma20_k[ik] * _VC, 0)

                alerts = {
                    "stopLoss": stop_loss_price,
                    "stopLossDist": round((current_price - stop_loss_price) / current_price * 100, 1),
                    "ma10": round(ma10_k[ik], 2) if ma10_k[ik] else None,
                    "ma10Dist": round((current_price - ma10_k[ik]) / current_price * 100, 1) if ma10_k[ik] else None,
                    "ma20": round(ma20_k[ik], 2) if ma20_k[ik] else None,
                    "ma20Dist": round((current_price - ma20_k[ik]) / current_price * 100, 1) if ma20_k[ik] else None,
                    "highRetreat": retreat_price,
                    "highRetreatDist": round((current_price - retreat_price) / current_price * 100, 1),
                    "peakSince": round(peak_since, 2),
                    "volCollapse": round(vol_collapse_5d, -4),  # 取整万
                    "volRatio": round(vr_k, 2),
                    "volDiverge": vr_k < 1.0,
                }

            holdings_with_price.append({
                **h,
                "name": name,
                "currentPrice": current_price,
                "marketValue": round(mv, 2),
                "pnl": round(pnl, 2),
                "pnlPct": round(pnl_pct, 2),
                "holdDays": max(0, hold_days),
                "latestDate": latest_date,
                "alerts": alerts,
            })
            holding_value += mv

    # 统计
    total_value = portfolio["cash"] + holding_value
    total_pnl = sum(h["pnl"] for h in holdings_with_price)
    total_pnl_pct = (total_pnl / (holding_value - total_pnl) * 100) if (holding_value - total_pnl) > 0 else 0

    # 已实现盈亏
    closed_trades = [l for l in logs if l["action"] == "sell"]
    realized_pnl = sum(l.get("pnl", 0) for l in closed_trades)
    wins = sum(1 for l in closed_trades if l.get("pnl", 0) > 0)
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0

    # ── 策略信号对比 ──
    comparison = []
    strategy_holds = []   # 策略持仓 (keeps)
    strategy_buys = []    # 策略买入信号 (buy_signals)

    history_file = os.path.join(os.path.dirname(__file__), "data", "trade_history.json")
    if os.path.exists(history_file):
        with open(history_file, "r", encoding="utf-8") as f:
            trade_history = json.load(f)
        if trade_history:
            latest_history = trade_history[-1]
            strategy_date = latest_history.get("date", "")
            bought_codes = {l["code"] for l in logs if l["action"] == "buy"}
            sold_codes = {l["code"] for l in logs if l["action"] == "sell"}
            held_codes = {h["code"] for h in portfolio.get("holdings", [])}

            # --- 策略持仓 (keeps) ---
            for k in latest_history.get("keeps", []):
                code = k["code"]
                holding = next((h for h in holdings_with_price if h["code"] == code), None)
                strategy_holds.append({
                    "code": code,
                    "name": k["name"],
                    "buyDate": k.get("buy_date", ""),
                    "buyPrice": k.get("buy_price", 0),
                    "strategyPrice": k.get("current_price") or k.get("buy_price", 0),
                    "pnlPct": k.get("profit_pct"),
                    "reason": k.get("buy_reason", "")[:40] if k.get("buy_reason") else "",
                    "followed": code in held_codes,
                    "yourPrice": holding["currentPrice"] if holding else None,
                    "yourPnlPct": holding["pnlPct"] if holding else None,
                })

            # --- 策略买入信号 (buy_signals) ---
            for i_s, s in enumerate(latest_history.get("buy_signals", [])):
                code = s["code"]
                buy_log = next((l for l in logs if l["code"] == code and l["action"] == "buy"), None)
                strategy_buys.append({
                    "rank": i_s + 1,
                    "code": code,
                    "name": s["name"],
                    "score": s.get("score", 0),
                    "close": s["close"],
                    "chg5d": s.get("chg_5d"),
                    "volRatio": s.get("vol_ratio"),
                    "upVolRatio": s.get("up_vol_ratio"),
                    "reason": s.get("reason", ""),
                    "followed": code in bought_codes,
                    "yourPrice": buy_log["price"] if buy_log else None,
                })

            # --- 实盘独有 (extra) ---
            all_strategy_codes = {item["code"] for item in strategy_holds}
            all_strategy_codes.update(item["code"] for item in strategy_buys)
            for h in holdings_with_price:
                if h["code"] not in all_strategy_codes:
                    comparison.append({
                        "type": "extra", "typeLabel": "额外",
                        "code": h["code"], "name": h["name"],
                        "strategyScore": None, "strategyPrice": None,
                        "chg5d": None, "volRatio": None, "reason": "",
                        "followed": "extra", "actualPrice": h["buy_price"],
                        "actualDate": h["buy_date"], "currentPrice": h["currentPrice"],
                        "pnlPct": h["pnlPct"], "holdDays": h["holdDays"],
                    })
        else:
            strategy_date = ""
    else:
        strategy_date = ""

    # 统计对比
    followed_holds = sum(1 for s in strategy_holds if s["followed"] is True)
    followed_buys = sum(1 for s in strategy_buys if s["followed"] is True)

    return jsonify({
        "portfolio": portfolio,
        "holdings": holdings_with_price,
        "logs": logs,
        "latestDate": latest_date,
        "strategyDate": strategy_date,
        "strategyHolds": strategy_holds,
        "strategyBuys": strategy_buys,
        "comparison": comparison,
        "stats": {
            "totalValue": round(total_value, 2),
            "holdingValue": round(holding_value, 2),
            "totalPnl": round(total_pnl, 2),
            "totalPnlPct": round(total_pnl_pct, 2),
            "realizedPnl": round(realized_pnl, 2),
            "closedTrades": len(closed_trades),
            "winRate": round(win_rate, 1),
            "strategyHoldCount": len(strategy_holds),
            "followedHolds": followed_holds,
            "strategyBuyCount": len(strategy_buys),
            "followedBuys": followed_buys,
            "extraCount": len([c for c in comparison if c["type"] == "extra"]),
        },
    })


@app.route("/api/trading/buy", methods=["POST"])
def api_trading_buy():
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    price = float(payload.get("price") or 0)
    shares = int(payload.get("shares") or 0)
    trade_date = payload.get("date") or datetime.now().strftime("%Y-%m-%d")
    reason = (payload.get("reason") or "").strip()

    # 决策复盘字段
    strategy_source = (payload.get("strategy_source") or "").strip()
    signal_rank = payload.get("signal_rank") or None
    market_signal = (payload.get("market_signal") or "").strip()
    market_score = payload.get("market_score") or None
    decision_note = (payload.get("decision_note") or "").strip()

    if not code or price <= 0 or shares < 100:
        return jsonify({"error": "参数无效"}), 400

    # 查名称
    with get_session() as sess:
        stock = sess.get(StockInfo, code)
        name = stock.name if stock else code

    # 更新 portfolio
    portfolio = _load_trading_portfolio()
    holding_entry = {
        "code": code,
        "name": name,
        "shares": shares,
        "buy_price": price,
        "buy_date": trade_date,
    }
    if strategy_source:
        holding_entry["strategy_source"] = strategy_source
    if decision_note:
        holding_entry["decision_note"] = decision_note
    portfolio["holdings"].append(holding_entry)
    portfolio["cash"] -= price * shares
    _save_trading_portfolio(portfolio)

    # 记录日志（含决策复盘字段）
    logs = _load_trade_log()
    log_entry = {
        "id": len(logs) + 1,
        "date": trade_date,
        "code": code,
        "name": name,
        "action": "buy",
        "price": price,
        "shares": shares,
        "amount": round(price * shares, 2),
        "reason": reason,
    }
    if strategy_source:
        log_entry["strategy_source"] = strategy_source
    if signal_rank is not None:
        log_entry["signal_rank"] = int(signal_rank)
    if market_signal:
        log_entry["market_signal"] = market_signal
    if market_score is not None:
        log_entry["market_score"] = float(market_score)
    if decision_note:
        log_entry["decision_note"] = decision_note
    logs.append(log_entry)
    _save_trade_log(logs)

    return jsonify({"ok": f"买入 {name}({code}) {shares}股 @ {price:.2f}"})


@app.route("/api/trading/sell", methods=["POST"])
def api_trading_sell():
    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    price = float(payload.get("price") or 0)
    shares = int(payload.get("shares") or 0)
    trade_date = payload.get("date") or datetime.now().strftime("%Y-%m-%d")
    reason = (payload.get("reason") or "").strip()

    if not code or price <= 0 or shares < 100:
        return jsonify({"error": "参数无效"}), 400

    # 查名称
    with get_session() as sess:
        stock = sess.get(StockInfo, code)
        name = stock.name if stock else code

    # 更新 portfolio：移除持仓（支持部分卖出）
    portfolio = _load_trading_portfolio()
    remaining_holdings = []
    sold_shares = 0
    sold_buy_price = 0
    for h in portfolio.get("holdings", []):
        if h["code"] == code:
            if shares >= h["shares"]:
                sold_shares = h["shares"]
                sold_buy_price = h["buy_price"]
                portfolio["cash"] += price * h["shares"]
                # 完全卖出，不保留
            else:
                sold_shares = shares
                sold_buy_price = h["buy_price"]
                portfolio["cash"] += price * shares
                remaining_holdings.append({**h, "shares": h["shares"] - shares})
        else:
            remaining_holdings.append(h)
    portfolio["holdings"] = remaining_holdings
    _save_trading_portfolio(portfolio)

    # 计算盈亏
    pnl = round((price - sold_buy_price) * sold_shares, 2)
    pnl_pct = round((price / sold_buy_price - 1) * 100, 2) if sold_buy_price > 0 else 0

    # 记录日志（含决策复盘字段）
    logs = _load_trade_log()
    sell_reason = (payload.get("sell_reason") or reason or "").strip()
    sell_note = (payload.get("decision_note") or "").strip()
    log_entry = {
        "id": len(logs) + 1,
        "date": trade_date,
        "code": code,
        "name": name,
        "action": "sell",
        "price": price,
        "shares": sold_shares,
        "amount": round(price * sold_shares, 2),
        "buyPrice": sold_buy_price,
        "pnl": pnl,
        "pnlPct": pnl_pct,
        "reason": sell_reason or reason,
    }
    if sell_note:
        log_entry["decision_note"] = sell_note
    logs.append(log_entry)
    _save_trade_log(logs)

    return jsonify({"ok": f"卖出 {name}({code}) {sold_shares}股 @ {price:.2f} | 盈亏 {pnl:+.0f}元 ({pnl_pct:+.1f}%)"})


@app.route("/api/trading/delete", methods=["POST"])
def api_trading_delete():
    payload = request.get_json(silent=True) or {}
    log_id = int(payload.get("id") or 0)
    if not log_id:
        return jsonify({"error": "缺少id"}), 400

    logs = _load_trade_log()
    target = next((l for l in logs if l.get("id") == log_id), None)
    if not target:
        return jsonify({"error": "记录不存在"}), 404

    # 如果是买入，需要回退持仓
    if target["action"] == "buy":
        portfolio = _load_trading_portfolio()
        portfolio["holdings"] = [
            h for h in portfolio.get("holdings", [])
            if not (h["code"] == target["code"] and h["buy_date"] == target["date"] and h["buy_price"] == target["price"])
        ]
        portfolio["cash"] += target["amount"]
        _save_trading_portfolio(portfolio)

    logs = [l for l in logs if l.get("id") != log_id]
    _save_trade_log(logs)
    return jsonify({"ok": f"已删除记录 #{log_id}"})


# ── 操作心得 API ──

# 大盘环境数据文件
MARKET_CONTEXT_FILE = os.path.join(os.path.dirname(__file__), "data", "market_context.json")

@app.route("/api/trading/market_context", methods=["GET"])
def api_trading_market_context():
    """获取每日大盘环境"""
    date_filter = request.args.get("date", "")
    if not os.path.exists(MARKET_CONTEXT_FILE):
        return jsonify({})
    with open(MARKET_CONTEXT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if date_filter and date_filter in data:
        return jsonify(data[date_filter])
    return jsonify(data)


@app.route("/api/trading/journal", methods=["GET"])
def api_trading_journal_list():
    """获取操作心得列表，可按日期或 trade_id 筛选"""
    date_filter = request.args.get("date", "")
    trade_id = request.args.get("trade_id", "")
    entries = _load_journal()
    if trade_id:
        entries = [e for e in entries if str(e.get("trade_id")) == str(trade_id)]
    elif date_filter:
        entries = [e for e in entries if e.get("date") == date_filter]
    # 按日期倒序
    entries.sort(key=lambda e: e.get("date", ""), reverse=True)
    return jsonify(entries)


@app.route("/api/trading/journal", methods=["POST"])
def api_trading_journal_save():
    """保存/更新一天的操作心得"""
    payload = request.get_json(silent=True) or {}
    date = (payload.get("date") or "").strip()
    if not date:
        return jsonify({"error": "日期不能为空"}), 400

    entries = _load_journal()
    trade_id = payload.get("trade_id") or None
    # 按 trade_id 去重（逐笔复盘），否则按 date 去重（按天复盘）
    if trade_id:
        existing = next((e for e in entries if str(e.get("trade_id")) == str(trade_id)), None)
    else:
        existing = next((e for e in entries if e.get("date") == date and not e.get("trade_id")), None)
    # 个股跟踪：[{code, name, status: "持有"|"加仓"|"减仓"|"观望"}]
    stocks = payload.get("stocks") or []
    stocks = [{"code": s["code"].strip(), "name": s.get("name", "").strip(), "status": s.get("status", "观望")}
              for s in stocks if s.get("code", "").strip()]

    # 复盘：对上一次的操作判断对错（可为空，第二天晚上填）
    review = payload.get("review") or {}
    if isinstance(review, str):
        review = {"verdict": review, "note": ""}

    entry = {
        "date": date,
        "trade_id": trade_id,
        "emotion": (payload.get("emotion") or "").strip(),
        "trade_type": (payload.get("trade_type") or "").strip(),
        "plan_followed": (payload.get("plan_followed") or "").strip(),
        "market_view": (payload.get("market_view") or "").strip(),
        "sector_view": (payload.get("sector_view") or "").strip(),
        "trade_rationale": (payload.get("trade_rationale") or "").strip(),
        "expectation": (payload.get("expectation") or "").strip(),
        "outcome": (payload.get("outcome") or "").strip(),
        "lessons": (payload.get("lessons") or "").strip(),
        "tomorrow_plan": (payload.get("tomorrow_plan") or "").strip(),
        "mistake_type": (payload.get("mistake_type") or "").strip(),
        "score": payload.get("score"),
        "review": {
            "verdict": review.get("verdict") or "",      # "对" / "错" / "半对" / ""
            "note": (review.get("note") or "").strip(),   # 复盘笔记
        },
        "stocks": stocks,
        "trades": payload.get("trades") or [],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if existing:
        existing.update(entry)
    else:
        entry["id"] = max((e.get("id", 0) for e in entries), default=0) + 1
        entries.append(entry)

    _save_journal(entries)

    # 复盘日志：每次保存都追加一条，方便恢复
    journal_log_file = os.path.join(os.path.dirname(__file__), "data", "journal_save_log.jsonl")
    log_entry = dict(entry)
    log_entry["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(journal_log_file, "a", encoding="utf-8") as lf:
        lf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return jsonify({"ok": f"已保存 {date} 操作心得", "entry": entry})


@app.route("/api/trading/journal/<int:entry_id>", methods=["DELETE"])
def api_trading_journal_delete(entry_id):
    entries = _load_journal()
    entries = [e for e in entries if e.get("id") != entry_id]
    _save_journal(entries)
    return jsonify({"ok": f"已删除 #{entry_id}"})


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
@app.route("/api/stocks/name/<code>")
def api_stock_name(code):
    """简单查股票名称"""
    with get_session() as sess:
        stock = sess.get(StockInfo, code.strip())
        if stock:
            return jsonify({"code": stock.code, "name": stock.name})
        return jsonify({"code": code, "name": ""})


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
        "market_cache_key": item["id"],
    } for item in list_strategies()])


@app.route("/api/backtest/market-overview")
def api_backtest_market_overview():
    max_positions = max(1, min(int(request.args.get("max_positions") or DEFAULT_MARKET_MAX_POSITIONS), 5))
    ranking = []
    missing = []
    for item in list_strategies():
        strategy_id = item["id"]
        # ETF 策略从 JSON 归档加载（id 以 etf_ 开头 或 META.type == "etf"）
        is_etf = item.get("type") == "etf" or strategy_id.startswith("etf_")
        if is_etf:
            result = _load_etf_strategy_result(strategy_id)
        else:
            result = load_latest_strategy_result(strategy_id)

        if not result:
            missing.append({
                "strategy_id": strategy_id,
                "strategy_name": item["name"],
            })
            continue
        summary = result["summary"]
        ranking.append({
            "strategy_id": strategy_id,
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
    result = load_latest_strategy_result("macd_cross")
    if not result:
        return jsonify({"error": "MACD 全市场回测结果还没有生成"}), 404
    return jsonify(result)


@app.route("/api/backtest/accumulation-market")
def api_backtest_accumulation_market():
    result = load_latest_strategy_result("accumulation_probe")
    if not result:
        return jsonify({"error": "吸筹试盘全市场回测结果还没有生成"}), 404
    return jsonify(result)


@app.route("/api/backtest/market/<strategy_id>")
def api_backtest_market(strategy_id: str):
    max_positions = max(1, min(int(request.args.get("max_positions") or DEFAULT_MARKET_MAX_POSITIONS), 5))
    try:
        strategy = get_strategy(strategy_id)
    except KeyError:
        return jsonify({"error": "策略不存在"}), 404

    # ETF 策略从 JSON 归档加载
    is_etf = strategy.META.get("type") == "etf" or strategy_id.startswith("etf_")
    if is_etf:
        result = _load_etf_strategy_result(strategy_id)
    else:
        result = load_latest_strategy_result(strategy_id)

    if not result:
        hint = "script/run_etf_backtest.py" if is_etf else "script/run_strategy_market_backtest.py"
        return jsonify({
            "error": f"{strategy.META['name']} 回测结果还没有生成，请先运行 {hint} --strategy {strategy_id}"
        }), 404
    return jsonify(result)


@app.route("/api/backtest/market/<strategy_id>/analysis")
def api_backtest_market_analysis(strategy_id: str):
    """返回策略深度分析数据"""
    try:
        strategy = get_strategy(strategy_id)
    except KeyError:
        return jsonify({"error": "策略不存在"}), 404

    is_etf = strategy.META.get("type") == "etf" or strategy_id.startswith("etf_")
    if is_etf:
        result = _load_etf_strategy_result(strategy_id)
    else:
        result = load_latest_strategy_result(strategy_id)

    if not result:
        return jsonify({"error": "回测结果还没有生成"}), 404

    trades = result.get("trades", [])
    curve = result.get("equity_curve", [])
    stocks = result.get("stock_summaries", [])

    # 1. 交易特征
    from collections import Counter, defaultdict
    from datetime import datetime as _dt

    win_trades = [t for t in trades if t.get("profit", 0) > 0]
    loss_trades = [t for t in trades if t.get("profit", 0) <= 0]
    avg_win = sum(t.get("profit_pct", 0) for t in win_trades) / max(1, len(win_trades))
    avg_loss = sum(t.get("profit_pct", 0) for t in loss_trades) / max(1, len(loss_trades))
    wl_ratio = abs(avg_win / avg_loss) if avg_loss else 0

    sell_reasons = Counter()
    for t in trades:
        r = t.get("sell_reason", "")
        if "到期" in r: sell_reasons["到期"] += 1
        elif "量价同步" in r: sell_reasons["量价同步"] += 1
        elif "期末持仓" in r: sell_reasons["期末持仓"] += 1
        elif "下穿" in r or "隧道" in r: sell_reasons["隧道信号"] += 1
        elif "回撤" in r or "止损" in r: sell_reasons["止损/回撤"] += 1
        else: sell_reasons["其他"] += 1

    hold_days = []
    for t in trades:
        try:
            b = _dt.strptime(t["buy_date"], "%Y-%m-%d")
            s = _dt.strptime(t.get("sell_date", t["buy_date"]), "%Y-%m-%d")
            if s > b: hold_days.append((s - b).days)
        except: pass

    trade_profile = {
        "total": len(trades), "wins": len(win_trades), "losses": len(loss_trades),
        "avg_win_pct": round(avg_win, 2), "avg_loss_pct": round(avg_loss, 2),
        "wl_ratio": round(wl_ratio, 2),
        "avg_hold": round(sum(hold_days) / max(1, len(hold_days)), 0),
        "med_hold": sorted(hold_days)[len(hold_days) // 2] if hold_days else 0,
        "max_hold": max(hold_days) if hold_days else 0,
        "sell_reasons": {k: v for k, v in sell_reasons.most_common()},
    }

    # 2. 年度表现
    yearly = defaultdict(lambda: {"t": 0, "w": 0, "p": 0.0, "s": 0, "e": 0})
    for t in trades:
        yr = t["buy_date"][:4]
        yearly[yr]["t"] += 1
        if t.get("profit", 0) > 0: yearly[yr]["w"] += 1
        yearly[yr]["p"] += t.get("profit", 0)
    for p in curve:
        yr = p["date"][:4]
        if yearly[yr]["s"] == 0: yearly[yr]["s"] = p["equity"]
        yearly[yr]["e"] = p["equity"]

    yearly_list = []
    for yr in sorted(yearly):
        d = yearly[yr]
        ret = ((d["e"] / d["s"] - 1) * 100) if d["s"] else 0
        yearly_list.append({
            "year": yr, "return_pct": round(ret, 1), "trades": d["t"],
            "win_rate": round(d["w"] / max(1, d["t"]) * 100, 0), "profit": round(d["p"], 0),
        })

    # 3. 回撤
    peak = 0; max_dd = 0; max_dd_start = ""; max_dd_end = ""
    dd_periods = []; in_dd = False; dd_start = ""; dd_peak2 = 0
    for p in curve:
        eq = p["equity"]
        if eq > peak: peak = eq
        dd = (eq - peak) / peak * 100 if peak else 0
        if dd < max_dd: max_dd = dd; max_dd_start = p["date"]; max_dd_end = p["date"]
    for p in curve:
        eq = p["equity"]
        if eq > dd_peak2: dd_peak2 = eq
        dd = (eq - dd_peak2) / dd_peak2 * 100 if dd_peak2 else 0
        if dd <= -10 and not in_dd: in_dd = True; dd_start = p["date"]
        elif dd > -3 and in_dd:
            in_dd = False
            worst = min((e["equity"] - dd_peak2) / dd_peak2 * 100
                       for e in curve if dd_start <= e["date"] <= p["date"])
            dd_periods.append({"start": dd_start, "end": p["date"], "worst": round(worst, 1)})

    # 4. 分类
    cats_def = [
        ("宽基-沪深300", ["沪深300"]), ("宽基-中证500", ["中证500"]),
        ("宽基-中证1000", ["中证1000"]), ("宽基-创业板", ["创业板"]),
        ("宽基-科创50", ["科创50"]), ("宽基-其他", ["上证50","A500","综指","中证2000"]),
        ("半导体", ["芯片","半导体"]), ("医药", ["医药","医疗","创新药"]),
        ("证券", ["证券","券商"]), ("消费", ["酒","消费","食品","家电"]),
        ("TMT", ["通信","5G","计算机","传媒","游戏","人工智能"]),
        ("能源", ["煤炭","电力","新能源","光伏","有色"]),
        ("红利", ["红利","低波"]), ("黄金", ["黄金"]),
    ]
    cat_list = []
    for cn, kws in cats_def:
        cs = [s for s in stocks if any(kw in s.get("name","") for kw in kws)]
        if not cs: continue
        tp = sum(s.get("profit",0) for s in cs); tt = sum(s.get("trade_count",0) for s in cs)
        tw = sum(s.get("wins",0) for s in cs)
        cat_list.append({"name":cn,"count":len(cs),"profit":round(tp,0),
                         "trades":tt,"win_rate":round(tw/max(1,tt)*100,0)})

    # 5. 集中度
    profits = sorted([s.get("profit",0) for s in stocks], reverse=True)
    tp = sum(p for p in profits if p > 0)
    top3 = sum(profits[:3]); top5 = sum(profits[:5]); top10 = sum(profits[:10])
    pos_n = sum(1 for s in stocks if s.get("profit",0) > 0)
    neg_n = sum(1 for s in stocks if s.get("profit",0) <= 0)

    return jsonify({
        "trade_profile": trade_profile,
        "yearly": yearly_list,
        "drawdown": {
            "max_dd_pct": round(max_dd, 1),
            "max_dd_range": f"{max_dd_start} ~ {max_dd_end}",
            "deep_periods": dd_periods[-5:],
        },
        "categories": cat_list,
        "concentration": {
            "pos_etfs": pos_n, "neg_etfs": neg_n,
            "total_pos_profit": round(tp, 0),
            "total_neg_profit": round(sum(p for p in profits if p < 0), 0),
            "top3_pct": round(top3/max(1,tp)*100, 0),
            "top5_pct": round(top5/max(1,tp)*100, 0),
            "top10_pct": round(top10/max(1,tp)*100, 0),
        },
    })


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
    from logic.backtest_cache import FIXED_START_DATE
    latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
    if not latest_date:
        return [], {}, latest_date

    cutoff = FIXED_START_DATE
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


# ── CSV data helpers (cached) ───────────────────────────────────
ETF_DIR = ROOT_DIR / "data" / "etf"
CONCEPT_DIR = ROOT_DIR / "data" / "concept"
LAB_CACHE_DIR = ROOT_DIR / "data" / "factor_lab" / "cache"
LAB_RESULT_DIR = ROOT_DIR / "data" / "factor_lab" / "result"
LAB_CACHE_VERSION = 4
LAB_FIXED_START_DATE = "2022-01-15"
LAB_FIXED_SOURCE = "concept"
LAB_FIXED_TOP_K = 5
LAB_FIXED_RESULT_PREFIX = "concept_gtja_alpha191_phase1_top5_2022-01-15"

# 全局缓存：一次性加载全部数据
_ETF_CACHE = None     # {code: {"name": str, "rows": [{...}]}}
_CONCEPT_CACHE = None


def _lab_data_signature(source):
    """Return a lightweight signature for CSV inputs used by factor-lab cache."""
    dirs = []
    if source in ("etf", "all"):
        dirs.append(ETF_DIR)
    if source in ("concept", "all"):
        dirs.append(CONCEPT_DIR)

    file_count = 0
    latest_mtime_ns = 0
    total_size = 0
    for base_dir in dirs:
        if not base_dir.exists():
            continue
        for root, _, files in os.walk(base_dir):
            for fn in files:
                if not fn.endswith(".csv"):
                    continue
                fp = Path(root) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                file_count += 1
                latest_mtime_ns = max(latest_mtime_ns, st.st_mtime_ns)
                total_size += st.st_size
    return {
        "files": file_count,
        "mtime": latest_mtime_ns,
        "size": total_size,
    }


def _lab_cache_path(cache_params):
    raw = json.dumps(cache_params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return LAB_CACHE_DIR / f"{key}.json", key


def _load_lab_backtest_cache(cache_params):
    path, key = _lab_cache_path(cache_params)
    if not path.exists():
        return None, key
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("cache", {})
        data["cache"].update({
            "hit": True,
            "key": key,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
        logger.info("Factor lab cache hit: %s", key)
        return data, key
    except Exception:
        logger.exception("Failed to read factor lab cache: %s", path)
        return None, key


def _load_lab_backtest_cache_by_key(cache_key):
    if not cache_key or len(cache_key) != 64 or any(c not in "0123456789abcdef" for c in cache_key.lower()):
        return None
    path = LAB_CACHE_DIR / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("cache", {})
        data["cache"].update({
            "hit": True,
            "key": cache_key,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
        return data
    except Exception:
        logger.exception("Failed to read factor lab cache by key: %s", path)
        return None


def _save_lab_backtest_cache(cache_params, data):
    LAB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path, key = _lab_cache_path(cache_params)
    payload = dict(data)
    payload["cache"] = {
        "hit": False,
        "key": key,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
        logger.info("Factor lab cache saved: %s", key)
    except Exception:
        logger.exception("Failed to write factor lab cache: %s", path)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return payload


def _lab_fixed_result_path(end_date):
    safe_end_date = (end_date or datetime.now().strftime("%Y-%m-%d")).replace("/", "-")
    return LAB_RESULT_DIR / f"{LAB_FIXED_RESULT_PREFIX}-{safe_end_date}.json"


def _latest_lab_fixed_result_path():
    if not LAB_RESULT_DIR.exists():
        return None
    paths = sorted(
        LAB_RESULT_DIR.glob(f"{LAB_FIXED_RESULT_PREFIX}-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return paths[0] if paths else None


def _load_lab_fixed_result():
    path = _latest_lab_fixed_result_path()
    if not path or not path.exists():
        return _load_lab_result_from_factor_dirs()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("result", {})
        data["result"].update({
            "path": str(path),
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
        return data
    except Exception:
        logger.exception("Failed to read factor lab fixed result: %s", path)
        return None


def _load_lab_result_from_factor_dirs():
    if not LAB_RESULT_DIR.exists():
        return None
    factors = {}
    factor_meta = {}
    item_names = {}
    top_k = None
    window = None
    item_count = None
    cache = {}
    result_paths = []

    for factor_dir in sorted(LAB_RESULT_DIR.glob("gtja-alpha-*")):
        if not factor_dir.is_dir():
            continue
        paths = sorted(factor_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not paths:
            continue
        path = paths[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to read factor lab factor result: %s", path)
            continue
        factor = data.get("factor")
        factor_data = data.get("factor_data")
        if not factor or not factor_data:
            continue
        factors[factor] = factor_data
        factor_meta.update(data.get("factor_meta") or {})
        if not item_names:
            item_names = data.get("item_names") or {}
        top_k = top_k or data.get("top_k")
        window = window or data.get("window")
        item_count = item_count or data.get("item_count")
        cache = cache or data.get("cache") or {}
        result_paths.append(str(path))

    if not factors:
        return None
    available = [fn for fn, value in factors.items() if value]
    return {
        "factors": factors,
        "factor_meta": factor_meta,
        "requested_factors": available,
        "available_factors": available,
        "top_k": top_k,
        "item_names": item_names,
        "item_count": item_count,
        "window": window,
        "cache": cache,
        "result": {
            "source": LAB_FIXED_SOURCE,
            "start_date": LAB_FIXED_START_DATE,
            "paths": result_paths,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "assembled_from_factor_dirs": True,
        },
    }


def _latest_lab_factor_result(factor_name):
    factor_no = factor_name.replace("gtja_alpha", "")
    factor_dir = LAB_RESULT_DIR / f"gtja-alpha-{factor_no}"
    if not factor_dir.exists():
        return None
    paths = sorted(factor_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return None
    try:
        with open(paths[0], "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("result", {})
        data["result"].update({
            "path": str(paths[0]),
            "created_at": datetime.fromtimestamp(paths[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
        return data
    except Exception:
        logger.exception("Failed to read factor lab single result: %s", paths[0])
        return None


def _save_lab_fixed_result(data, end_date):
    LAB_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = _lab_fixed_result_path(end_date)
    payload = dict(data)
    payload["result"] = {
        "source": LAB_FIXED_SOURCE,
        "start_date": LAB_FIXED_START_DATE,
        "end_date": end_date,
        "top_k": LAB_FIXED_TOP_K,
        "path": str(path),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)
    _save_lab_factor_results(payload, end_date)
    logger.info("Factor lab fixed result saved: %s", path)
    return payload


def _save_lab_factor_results(data, end_date):
    factors = data.get("factors") or {}
    meta = data.get("factor_meta") or {}
    item_names = data.get("item_names") or {}
    for factor_name, factor_data in factors.items():
        if not factor_data:
            continue
        factor_no = factor_name.replace("gtja_alpha", "")
        factor_dir = LAB_RESULT_DIR / f"gtja-alpha-{factor_no}"
        factor_dir.mkdir(parents=True, exist_ok=True)
        factor_path = factor_dir / f"gtja-alpha-{factor_no}_top{data.get('top_k', LAB_FIXED_TOP_K)}_{LAB_FIXED_START_DATE}-{end_date}.json"
        payload = {
            "factor": factor_name,
            "factor_data": factor_data,
            "factor_meta": {factor_name: meta.get(factor_name, {})},
            "item_names": item_names,
            "top_k": data.get("top_k"),
            "window": data.get("window"),
            "item_count": data.get("item_count"),
            "cache": data.get("cache", {}),
            "result": {
                "source": LAB_FIXED_SOURCE,
                "start_date": LAB_FIXED_START_DATE,
                "end_date": end_date,
                "path": str(factor_path),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        tmp_path = factor_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, factor_path)


def _load_all_csv(base_dir, code_key="code", name_key="name"):
    """一次性读取 base_dir 下所有 CSV，返回 {code: {name, rows}}"""
    import csv as _csv
    from collections import defaultdict
    data = defaultdict(lambda: {"name": "", "rows": []})
    years = sorted([d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))])
    for year in years:
        year_dir = os.path.join(base_dir, str(year))
        if not os.path.isdir(year_dir):
            continue
        for fn in sorted(os.listdir(year_dir)):
            if not fn.endswith(".csv"):
                continue
            fp = os.path.join(year_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8-sig") as f:
                    for row in _csv.DictReader(f):
                        code = row[code_key]
                        name = row[name_key]
                        if not data[code]["name"]:
                            data[code]["name"] = name
                        data[code]["rows"].append({
                            "trade_date": row["trade_date"],
                            "open": float(row["open"]) if row.get("open") else None,
                            "high": float(row["high"]) if row.get("high") else None,
                            "low": float(row["low"]) if row.get("low") else None,
                            "close": float(row["close"]) if row.get("close") else None,
                            "volume": float(row["volume"]) if row.get("volume") else None,
                            "amount": float(row["amount"]) if row.get("amount") else None,
                        })
            except Exception:
                pass
    for code in data:
        data[code]["rows"].sort(key=lambda r: r["trade_date"])
    return dict(data)


def _ensure_cache():
    """确保缓存已加载"""
    global _ETF_CACHE, _CONCEPT_CACHE
    if _ETF_CACHE is None:
        logger.info("Loading ETF cache from CSV...")
        _ETF_CACHE = _load_all_csv(ETF_DIR, "code", "name")
        logger.info("ETF cache: %d items", len(_ETF_CACHE))
    if _CONCEPT_CACHE is None:
        logger.info("Loading concept cache from CSV...")
        _CONCEPT_CACHE = _load_all_csv(CONCEPT_DIR, "concept_code", "concept_name")
        logger.info("Concept cache: %d items", len(_CONCEPT_CACHE))


def _filter_rows(rows, start_date=None, end_date=None):
    """按日期过滤，返回 rows 列表"""
    if not start_date and not end_date:
        return rows
    result = []
    for r in rows:
        td = r["trade_date"]
        if start_date and td < start_date:
            continue
        if end_date and td > end_date:
            continue
        result.append(r)
    return result


# ── concepts (CSV-based) ────────────────────────────────────────
@app.route("/api/concepts")
def api_concepts():
    _ensure_cache()
    return jsonify([{"code": c, "name": d["name"]} for c, d in _CONCEPT_CACHE.items()])


@app.route("/api/concepts/<concept_code>/daily")
def api_concept_daily(concept_code: str):
    limit = request.args.get("limit", 1000, type=int)
    _ensure_cache()
    entry = _CONCEPT_CACHE.get(concept_code)
    if not entry:
        return jsonify([])
    rows = entry["rows"]
    return jsonify(rows[-limit:])


@app.route("/api/concepts/<concept_code>/stocks")
def api_concept_stocks(concept_code: str):
    # stock_concept 关系表可能不再使用，返回空
    return jsonify({"concept": {"code": concept_code, "name": ""}, "stocks": []})


# ── strategy lab ────────────────────────────────────────────────
@app.route("/strategy-lab")
def page_strategy_lab():
    return render_template("strategy_lab.html")


@app.route("/strategy-lab/detail")
def page_strategy_lab_detail():
    return render_template("strategy_lab_detail.html")


@app.route("/api/lab/detail")
def api_lab_detail():
    cache_key = (request.args.get("cache") or "").strip()
    factor = (request.args.get("factor") or "").strip()
    data = _load_lab_backtest_cache_by_key(cache_key)
    if not data:
        single = _latest_lab_factor_result(factor)
        if single:
            return jsonify({
                "factor": factor,
                "factor_data": single.get("factor_data"),
                "factor_meta": single.get("factor_meta", {}),
                "factor_names": {factor: (single.get("factor_meta", {}).get(factor, {}).get("name") or factor)},
                "available_factors": [factor],
                "item_names": single.get("item_names", {}),
                "top_k": single.get("top_k"),
                "window": single.get("window"),
                "item_count": single.get("item_count"),
                "cache": single.get("cache", {}),
                "result": single.get("result", {}),
            })
        return jsonify({"error": "缓存结果不存在，请先在实验室重新跑一次回测"}), 404
    factors = data.get("factors") or {}
    fdata = factors.get(factor)
    if not fdata:
        return jsonify({"error": "因子结果不存在"}), 404
    return jsonify({
        "factor": factor,
        "factor_data": fdata,
        "factor_meta": data.get("factor_meta", {}),
        "factor_names": {k: v.get("name", k) for k, v in (data.get("factor_meta") or {}).items()},
        "available_factors": data.get("available_factors", []),
        "item_names": data.get("item_names", {}),
        "top_k": data.get("top_k"),
        "window": data.get("window"),
        "item_count": data.get("item_count"),
        "cache": data.get("cache", {}),
    })


@app.route("/api/lab/fixed-result")
def api_lab_fixed_result():
    data = _load_lab_fixed_result()
    if not data:
        return jsonify({"error": "固定结果还没有生成"}), 404
    return jsonify(data)


@app.route("/api/lab/fixed-result/generate", methods=["POST"])
def api_lab_generate_fixed_result():
    end_date = request.args.get("end_date") or datetime.now().strftime("%Y-%m-%d")
    payload = {
        "source": LAB_FIXED_SOURCE,
        "start_date": LAB_FIXED_START_DATE,
        "end_date": end_date,
        "top_k": LAB_FIXED_TOP_K,
        "factors": list(FACTOR_META.keys()),
    }
    with app.test_client() as client:
        resp = client.post("/api/lab/backtest", json=payload)
        data = resp.get_json()
    if not data or data.get("error"):
        return jsonify(data or {"error": "固定结果生成失败"}), 500
    data = _save_lab_fixed_result(data, end_date)
    return jsonify(data)


def _list_etfs_from_csv():
    _ensure_cache()
    return [{"code": c, "name": d["name"]} for c, d in _ETF_CACHE.items()]


def _list_concepts_from_csv():
    _ensure_cache()
    return [{"code": c, "name": d["name"]} for c, d in _CONCEPT_CACHE.items()]


@app.route("/api/lab/etfs")
def api_lab_etfs():
    return jsonify({"items": _list_etfs_from_csv()})


@app.route("/api/lab/concepts")
def api_lab_concepts():
    return jsonify({"items": _list_concepts_from_csv()})


def _read_etf_daily(code, limit=2000, start_date=None, end_date=None):
    """读取单个 ETF 日线（从缓存）"""
    _ensure_cache()
    entry = _ETF_CACHE.get(code)
    if not entry:
        return []
    rows = _filter_rows(entry["rows"], start_date, end_date)
    if start_date or end_date:
        return rows
    return rows[-limit:]


def _read_concept_daily(code, limit=2000, start_date=None, end_date=None):
    """读取单个概念日线（从缓存）"""
    _ensure_cache()
    entry = _CONCEPT_CACHE.get(code)
    if not entry:
        return []
    rows = _filter_rows(entry["rows"], start_date, end_date)
    if start_date or end_date:
        return rows
    return rows[-limit:]


@app.route("/api/lab/etf/<code>/daily")
def api_lab_etf_daily(code: str):
    limit = request.args.get("limit", 2000, type=int)
    return jsonify(_read_etf_daily(code, limit))


@app.route("/api/lab/concept/<code>/daily")
def api_lab_concept_daily(code: str):
    limit = request.args.get("limit", 2000, type=int)
    return jsonify(_read_concept_daily(code, limit))


# ── factor engine ───────────────────────────────────────────────

def _sma(values, period):
    """简单移动平均"""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _std(values, period):
    """标准差"""
    import math
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    return math.sqrt(variance)


# ── Alpha101 helpers ────────────────────────────────────────────

def _rank(vals):
    """百分位排名 [0, 1]"""
    if not vals:
        return []
    n = len(vals)
    if n == 1:
        return [0.5]
    sorted_idx = sorted(range(n), key=lambda i: vals[i])
    ranks = [0.0] * n
    for rank_pos, idx in enumerate(sorted_idx):
        ranks[idx] = rank_pos / (n - 1)
    return ranks


def _ts_rank(values, window):
    """时间序列滚动排名，返回最后一个值"""
    if len(values) < window:
        return None
    seg = values[-window:]
    r = _rank(seg)
    return r[-1]


def _ts_sum(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:])


def _ts_min(values, window):
    if len(values) < window:
        return None
    return min(values[-window:])


def _ts_max(values, window):
    if len(values) < window:
        return None
    return max(values[-window:])


def _delta(values, lag=1):
    """values[-1] - values[-1-lag]"""
    if len(values) <= lag:
        return None
    return values[-1] - values[-1 - lag]


def _delay(values, lag=1):
    """前一期的值"""
    if len(values) <= lag:
        return None
    return values[-1 - lag]


def _correlation(xs, ys, window):
    """滚动窗口内 Pearson 相关系数"""
    if len(xs) < window or len(ys) < window:
        return None
    x = xs[-window:]
    y = ys[-window:]
    n = window
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = (sum((v - mx) ** 2 for v in x) / n) ** 0.5
    sy = (sum((v - my) ** 2 for v in y) / n) ** 0.5
    if sx == 0 or sy == 0:
        return 0
    return cov / (n * sx * sy)


def _scale(a):
    """sum(abs(x))"""
    return sum(abs(x) for x in [a]) if a is not None else 0


def _signed_power(x, a):
    """sign(x) * |x|^a"""
    if x is None or a is None:
        return None
    import math
    return math.copysign(abs(x) ** a, x)


def _alpha_annual_return(close, window=252):
    """252日年化收益 (close用)"""
    if len(close) < window:
        return None
    r = (close[-1] / close[-window - 1]) - 1 if close[-window - 1] > 0 else None
    return r


def _alpha_product(values, window):
    """滚动乘积"""
    if len(values) < window:
        return None
    import math
    p = 1.0
    for v in values[-window:]:
        p *= max(v, 1e-10)
    return p


def _ts_mean(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _calc_factors(rows, factors, window):
    """对一组日线数据计算因子值。

    返回: {factor_name: value_or_None}
    """
    if len(rows) < max(60, window):
        return None

    closes = [r["close"] for r in rows]
    volumes = [r["volume"] or 0 for r in rows]
    highs = [r["high"] for r in rows]

    # 日收益率
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] and closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
        else:
            returns.append(0)

    result = {}

    for f in factors:
        if f == "momentum":
            # N日动量: (close[-1] - close[-N]) / close[-N]
            n = min(20, window)
            if len(closes) > n and closes[-n - 1] and closes[-n - 1] > 0:
                result[f] = (closes[-1] - closes[-n - 1]) / closes[-n - 1]
            else:
                result[f] = None

        elif f == "volatility":
            # 年化波动率: std(returns, N) * sqrt(252)
            n = min(20, len(returns))
            std_r = _std(returns, n)
            result[f] = std_r * (252 ** 0.5) if std_r is not None else None

        elif f == "vol_ratio":
            # 量比: sma(volume, 5) / sma(volume, 20)
            vol5 = _sma(volumes, min(5, len(volumes)))
            vol20 = _sma(volumes, min(20, len(volumes)))
            result[f] = vol5 / vol20 if vol5 and vol20 and vol20 > 0 else None

        elif f == "sharpe":
            # 夏普比率 (60日): mean(returns) / std(returns) * sqrt(252)
            n = min(60, len(returns))
            if n > 5:
                r = returns[-n:]
                mean_r = sum(r) / n
                std_r = _std(returns, n)
                result[f] = (mean_r / std_r) * (252 ** 0.5) if std_r and std_r > 0 else None
            else:
                result[f] = None

        elif f == "pv_corr":
            # 量价相关性: correlation(close, volume) over N days
            n = min(20, len(closes))
            if n > 5:
                c = closes[-n:]
                v = volumes[-n:]
                mean_c = sum(c) / n
                mean_v = sum(v) / n
                cov = sum((c[i] - mean_c) * (v[i] - mean_v) for i in range(n))
                std_c = (sum((x - mean_c) ** 2 for x in c) / n) ** 0.5
                std_v = (sum((x - mean_v) ** 2 for x in v) / n) ** 0.5
                if std_c > 0 and std_v > 0:
                    result[f] = cov / (n * std_c * std_v)
                else:
                    result[f] = None
            else:
                result[f] = None

        elif f == "max_dd":
            # 最大回撤: 窗口内从高点到最低点的最大跌幅
            n = min(60, len(closes))
            if n > 5:
                c = closes[-n:]
                peak = c[0]
                max_dd = 0
                for price in c:
                    if price > peak:
                        peak = price
                    dd = (price - peak) / peak if peak > 0 else 0
                    if dd < max_dd:
                        max_dd = dd
                result[f] = max_dd  # negative value
            else:
                result[f] = None

        elif f == "up_ratio":
            # 涨跌比: up_days / down_days over N
            n = min(20, len(returns))
            if n > 0:
                r = returns[-n:]
                up = sum(1 for x in r if x > 0)
                down = sum(1 for x in r if x < 0)
                result[f] = up / down if down > 0 else (up if up > 0 else 1.0)
            else:
                result[f] = None

        elif f == "chg_std":
            n = min(20, len(returns))
            std_r = _std(returns, n)
            result[f] = std_r if std_r is not None else None

        # ── Alpha 101 因子 ──
        elif f == "alpha006":
            # -correlation(open, volume, 10)
            n = min(10, len(closes))
            c = _correlation([r["open"] for r in rows], volumes, n)
            result[f] = -c if c is not None else None

        elif f == "alpha009":
            # if ts_min(delta(close,1),5) > 0: delta(close,1)
            # elif ts_max(delta(close,1),5) < 0: delta(close,1)
            # else: -delta(close,1)
            d1 = _delta(closes, 1)
            d1s = [_delta(closes[:i+1], 1) for i in range(len(closes))]
            d1s_clean = [v for v in d1s if v is not None]
            tmin = _ts_min(d1s_clean, 5) if len(d1s_clean) >= 5 else None
            tmax = _ts_max(d1s_clean, 5) if len(d1s_clean) >= 5 else None
            if tmin is not None and tmin > 0:
                result[f] = d1
            elif tmax is not None and tmax < 0:
                result[f] = d1
            else:
                result[f] = -d1 if d1 is not None else None

        elif f == "alpha012":
            # sign(delta(volume,1)) * -delta(close,1)
            import math
            dv = _delta(volumes, 1)
            dc = _delta(closes, 1)
            if dv is not None and dc is not None:
                result[f] = math.copysign(1, dv) * (-dc) if dv != 0 else 0
            else:
                result[f] = None

        elif f == "alpha032":
            # scale(((sum(close,7)/7 - close) + correlation(vwap, delay(close,5), 20)))
            n = min(20, len(closes))
            sm7 = _ts_sum(closes, 7)
            mean_rev = (sm7 / 7.0 - closes[-1]) if sm7 else 0
            # vwap ≈ amount / volume
            vwaps = []
            for r in rows:
                if r.get("amount") and r.get("volume") and r["volume"] > 0:
                    vwaps.append(r["amount"] / r["volume"])
                else:
                    vwaps.append((r["high"] + r["low"] + r["close"]) / 3)
            d5 = _delay(closes, 5)
            # Build delayed close series
            delayed = [None] * 5 + closes[:-5] if len(closes) > 5 else []
            delayed_clean = [v for v in delayed if v is not None]
            c = _correlation(vwaps, delayed_clean, min(n, len(delayed_clean)))
            result[f] = (mean_rev + (c if c else 0)) if mean_rev is not None else None

        elif f == "alpha034":
            # rank((1 - rank(stddev(returns,2)/stddev(returns,5))) + (1 - rank(delta(close,1))))
            pass  # needs cross-sectional rank, stubbed for now
            result[f] = None

        elif f == "alpha038":
            # -rank(ts_rank(close,10)) * rank(correlation(close,volume,10))
            n = min(10, len(closes))
            tr = _ts_rank(closes, n)
            c = _correlation(closes, volumes, n)
            # approximate: use percentile as rank proxy
            result[f] = -(tr * c) if tr is not None and c is not None else None

        elif f == "alpha042":
            # rank(vwap - close) / rank(vwap + close), approximated with time-series ranks
            n = min(20, len(closes))
            if n > 5:
                vwaps = []
                for r in rows:
                    if r.get("amount") and r.get("volume") and r["volume"] > 0:
                        vwaps.append(r["amount"] / r["volume"])
                    else:
                        vwaps.append((r["high"] + r["low"] + r["close"]) / 3)
                spread = [vwaps[i] - closes[i] for i in range(len(closes))]
                level = [vwaps[i] + closes[i] for i in range(len(closes))]
                spread_rank = _rank(spread[-n:])[-1]
                level_rank = max(_rank(level[-n:])[-1], 1e-6)
                result[f] = spread_rank / level_rank
            else:
                result[f] = None

        elif f == "alpha046":
            # delta(close,20)/20 > 0.05 ? -rank(stddev(close,5)) : 1
            d20 = _delta(closes, 20)
            if d20 is not None:
                d20_pct = d20 / closes[-21] if len(closes) > 20 and closes[-21] > 0 else 0
                if d20_pct > 0.05:
                    n = min(5, len(closes))
                    std_c = _std(closes, n)
                    result[f] = -std_c if std_c else None
                else:
                    result[f] = 1.0
            else:
                result[f] = None

        elif f == "alpha054":
            # (-1 * (low - close) * (open^5)) / ((low - high) * (close^5))
            r = rows[-1]
            lo, hi, op, cl = r["low"], r["high"], r["open"], r["close"]
            if hi != lo and cl != 0:
                num = -1 * (lo - cl) * (op ** 5)
                den = (lo - hi) * (cl ** 5)
                result[f] = num / den if den != 0 else 0
            else:
                result[f] = None

        elif f == "alpha057":
            # RSI-style mean reversion
            n = min(20, len(closes))
            sm20 = _ts_sum(closes, n)
            if sm20:
                ma20 = sm20 / n
                d1 = _delta(closes, 1)
                d5_sum = _ts_sum([abs(d) for d in returns[-5:]], 5) if len(returns) >= 5 else None
                if d1 is not None:
                    result[f] = ma20 / closes[-1] if closes[-1] > 0 else None  # 均值偏离度
                else:
                    result[f] = None
            else:
                result[f] = None

        elif f == "alpha065":
            # rank(correlation(close,adv20,15) < correlation(close,adv20,5))
            n15 = min(15, len(closes))
            n5 = min(5, len(closes))
            # adv20 ≈ sma(volume,20)
            adv20 = []
            for i in range(len(volumes)):
                if i >= 19:
                    adv20.append(sum(volumes[i-19:i+1]) / 20)
                else:
                    adv20.append(sum(volumes[:i+1]) / (i+1))
            c15 = _correlation(closes, adv20, n15)
            c5 = _correlation(closes, adv20, n5)
            if c15 is not None and c5 is not None:
                result[f] = 1.0 if c15 < c5 else 0.0
            else:
                result[f] = None

        elif f == "alpha081":
            # rank(log(product(rank(correlation(vwap, sum(adv20,20), 10)), 10)))
            # Simplified: vwap-volume momentum
            vwaps = []
            for r in rows:
                if r.get("amount") and r.get("volume") and r["volume"] > 0:
                    vwaps.append(r["amount"] / r["volume"])
                else:
                    vwaps.append((r["high"] + r["low"] + r["close"]) / 3)
            n = min(10, len(closes))
            # adv20 sum
            adv20_vals = []
            for i in range(len(volumes)):
                if i >= 19:
                    adv20_vals.append(sum(volumes[i-19:i+1]) / 20)
                else:
                    adv20_vals.append(sum(volumes[:i+1]) / (i+1))
            adv20_sum = _ts_sum(adv20_vals, min(20, len(adv20_vals)))
            c = _correlation(vwaps, adv20_vals, n)
            import math
            if c is not None and c > 0:
                result[f] = math.log(max(c, 0.0001))
            else:
                result[f] = None

        elif f == "alpha101":
            # (close - open) / ((high - low) + 0.001)
            r = rows[-1]
            result[f] = (r["close"] - r["open"]) / (r["high"] - r["low"] + 0.001)

        else:
            result[f] = None

    return result


@app.route("/api/lab/analyze", methods=["POST"])
def api_lab_analyze():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])  # [{code, name, type: 'etf'|'concept'}]
    factors = payload.get("factors", ["momentum", "volatility", "vol_ratio", "sharpe"])
    start_date = payload.get("start_date", "")
    end_date = payload.get("end_date", "")
    sort_by = payload.get("sort_by", "composite")

    if not items:
        return jsonify({"error": "请选择至少一个标的"}), 400
    if len(items) > 15:
        return jsonify({"error": "最多选择15个标的"}), 400

    results = []
    for item in items:
        code = item["code"]
        item_type = item.get("type", "etf")

        if item_type == "etf":
            rows = _read_etf_daily(code, start_date=start_date, end_date=end_date)
        else:
            rows = _read_concept_daily(code, start_date=start_date, end_date=end_date)

        if not rows or len(rows) < 10:
            continue

        # 估算交易日数用于因子计算
        n_rows = len(rows)
        window = min(n_rows, 120)

        fv = _calc_factors(rows, factors, window)
        if fv is None:
            continue

        results.append({
            "code": code,
            "name": item.get("name", code),
            "type": item_type,
            "factors": fv,
        })

    if not results:
        return jsonify({"error": "没有足够的数据"}), 400

    # normalize factors and compute composite score
    factor_vals = {f: [] for f in factors}
    for r in results:
        for f in factors:
            v = r["factors"].get(f)
            if v is not None:
                factor_vals[f].append(v)

    # min-max normalize per factor
    norm = {}
    for f in factors:
        vals = factor_vals[f]
        if not vals:
            norm[f] = {"min": 0, "max": 1, "range": 1}
            continue
        mn, mx = min(vals), max(vals)
        rng = mx - mn if mx != mn else 1
        if f == "max_dd":
            mn, mx = mx, mn
            rng = abs(rng)
        norm[f] = {"min": mn, "max": mx, "range": rng}

    for r in results:
        score = 0
        count = 0
        for f in factors:
            v = r["factors"].get(f)
            if v is None:
                continue
            n = norm[f]
            nv = (v - n["min"]) / n["range"] if n["range"] != 0 else 0.5
            score += nv
            count += 1
        r["score"] = score / count if count > 0 else 0

    # sort
    if sort_by == "composite":
        results.sort(key=lambda r: r["score"], reverse=True)
    elif sort_by in factors:
        results.sort(
            key=lambda r: r["factors"].get(sort_by, -9999) or -9999,
            reverse=sort_by != "max_dd"
        )

    # 源标签
    has_etf = any(r["type"] == "etf" for r in results)
    has_concept = any(r["type"] == "concept" for r in results)
    if has_etf and has_concept:
        source_label = "ETF + 概念"
    elif has_etf:
        source_label = "ETF指数"
    else:
        source_label = "概念板块"

    return jsonify({
        "source_label": source_label,
        "window": f"{start_date} ~ {end_date}",
        "factors": factors,
        "sort_by": sort_by,
        "results": results,
    })


# ── factor backtest engine ──────────────────────────────────────

FACTOR_META = {
    fn: {"name": f"GTJA {fn[-3:]}", "higher_better": True, "group": "GTJA Alpha191 Phase1"}
    for fn in GTJA_ALPHA_PHASE1_ACTIVE
}


def _rolling_factor(rows, factor_name, idx, window=60):
    """在 rows 的 idx 位置，用前 window 根 bar 计算因子值"""
    if idx < window:
        return None
    segment = rows[idx - window:idx + 1]
    fv = _calc_factors(segment, [factor_name], window)
    return fv.get(factor_name) if fv else None


def _precompute_all_factors(rows, factor_names, lookback=60):
    """一次遍历计算所有因子时序。
    返回: {factor_name: [{date, value, close}]}
    """
    if len(rows) < lookback:
        return {}
    result = {f: [] for f in factor_names}
    for idx in range(lookback, len(rows)):
        fv = _calc_factors(rows[:idx + 1], factor_names, lookback)
        if fv is None:
            continue
        dt = rows[idx]["trade_date"]
        cl = rows[idx]["close"]
        for fn in factor_names:
            v = fv.get(fn)
            if v is not None:
                result[fn].append({"date": dt, "value": v, "close": cl})
    return result


def _run_factor_backtest_cached(factor_series, factor_name, start_date, end_date, top_k):
    """使用预计算的因子时序跑回测。
    factor_series: {code: [{date, value, close}]}
    """
    higher_better = FACTOR_META.get(factor_name, {}).get("higher_better", True)

    # 建立 date→index 映射 + 收集交易日
    date_index = {}
    all_dates = set()
    for code, fs in factor_series.items():
        date_index[code] = {p["date"]: i for i, p in enumerate(fs)}
        for p in fs:
            if start_date <= p["date"] <= end_date:
                all_dates.add(p["date"])
    all_dates = sorted(all_dates)
    if len(all_dates) < 30:
        return [], []

    # 逐日回测
    equity = 1.0
    equity_curve = []
    prev_top_codes = set()
    prev_close = {}
    entry_info = {}
    trades = []
    day_idx = 0

    for dt in all_dates:
        scores = []
        day_prices = {}
        for code, fs in factor_series.items():
            di = date_index[code].get(dt)
            if di is None:
                continue
            p = fs[di]
            scores.append((code, p["value"]))
            day_prices[code] = p["close"]

        if len(scores) < top_k:
            continue

        daily_ret = 0
        ret_count = 0
        for code in prev_top_codes:
            p_close = day_prices.get(code)
            if not p_close:
                continue
            pc = prev_close.get(code)
            if pc and pc > 0:
                daily_ret += (p_close / pc - 1)
                ret_count += 1
        if ret_count > 0:
            daily_ret /= ret_count
            equity *= (1 + daily_ret)

        scores.sort(key=lambda x: x[1], reverse=higher_better)
        top_codes = set(c for c, _ in scores[:top_k])
        rank_map = {code: i + 1 for i, (code, _) in enumerate(scores[:top_k])}
        score_map = {code: value for code, value in scores[:top_k]}

        bought = top_codes - prev_top_codes
        sold = prev_top_codes - top_codes
        for code in sold:
            h = entry_info.pop(code, None)
            sell_price = day_prices.get(code)
            if h and h.get("entry_price") and sell_price:
                pnl_pct = (sell_price / h["entry_price"] - 1) * 100
                trades.append({
                    "code": code, "buy_date": h["entry_date"], "sell_date": dt,
                    "buy_price": round(h["entry_price"], 2), "sell_price": round(sell_price, 2),
                    "profit_pct": round(pnl_pct, 2), "hold_days": day_idx - h["entry_idx"],
                })
        for code in bought:
            entry_info[code] = {"entry_date": dt, "entry_price": day_prices.get(code), "entry_idx": day_idx}

        for code in top_codes:
            if code in day_prices:
                prev_close[code] = day_prices[code]

        prev_top_codes = top_codes
        day_idx += 1
        equity_curve.append({
            "date": dt, "equity": round(equity, 4),
            "return": round((equity - 1) * 100, 2),
            "holdings": [{
                "code": code,
                "rank": rank_map.get(code),
                "score": round(float(score_map.get(code, 0)), 6),
                "close": round(float(day_prices[code]), 2) if code in day_prices and day_prices[code] is not None else None,
            } for code in sorted(top_codes, key=lambda c: rank_map.get(c, 999))],
        })

    if equity_curve and prev_top_codes:
        last_date = equity_curve[-1]["date"]
        for code, h in entry_info.items():
            sell_price = prev_close.get(code)
            if sell_price and h.get("entry_price"):
                pnl_pct = (sell_price / h["entry_price"] - 1) * 100
                trades.append({
                    "code": code, "buy_date": h["entry_date"], "sell_date": last_date,
                    "buy_price": round(h["entry_price"], 2), "sell_price": round(sell_price, 2),
                    "profit_pct": round(pnl_pct, 2), "hold_days": day_idx - h["entry_idx"],
                })

    return equity_curve, trades


def _precompute_factor_series_single(rows, factor_name, lookback=60):
    """预计算单个因子的时序（兼容旧接口）"""
    if len(rows) < lookback:
        return []
    series = []
    for idx in range(lookback, len(rows)):
        fv = _rolling_factor(rows, factor_name, idx, window=lookback)
        if fv is not None:
            series.append({
                "date": rows[idx]["trade_date"],
                "value": fv,
                "close": rows[idx]["close"],
            })
    return series


def _precompute_factors_numpy(rows, factor_names):
    """用 numpy 向量化一次性计算所有因子时序。"""
    import numpy as np
    n = len(rows)
    if n < 60:
        return {}

    closes = np.array([r["close"] for r in rows])
    highs = np.array([r["high"] for r in rows])
    lows = np.array([r["low"] for r in rows])
    opens = np.array([r["open"] for r in rows])
    volumes = np.array([r["volume"] or 0 for r in rows])
    amounts = np.array([r["amount"] or 0 for r in rows])
    dates = [r["trade_date"] for r in rows]

    # 日收益率
    returns = np.zeros(n)
    returns[1:] = (closes[1:] - closes[:-1]) / np.maximum(closes[:-1], 1e-10)

    def _safe_corrcoef(x, y):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if np.count_nonzero(mask) < 3:
            return np.nan
        x = x[mask]
        y = y[mask]
        if np.std(x) <= 0 or np.std(y) <= 0:
            return np.nan
        return np.corrcoef(x, y)[0, 1]

    def _vwap_array(amount, volume, high, low, close):
        fallback = (high + low + close) / 3
        return np.divide(amount, volume, out=fallback.astype(float, copy=True), where=volume > 0)

    def _percent_rank(window, value):
        window = np.asarray(window, dtype=float)
        window = window[np.isfinite(window)]
        if len(window) < 2 or not np.isfinite(value):
            return 0.5
        return float(np.searchsorted(np.sort(window), value, side="right") / len(window))

    result = {f: [] for f in factor_names}
    lookback = 60

    for idx in range(lookback, n):
        seg = slice(idx - lookback, idx)
        c = closes[idx - lookback:idx + 1]
        r = returns[idx - lookback + 1:idx + 1]
        v = volumes[idx - lookback:idx + 1]
        h = highs[idx - lookback:idx + 1]
        lo = lows[idx - lookback:idx + 1]
        o = opens[idx - lookback:idx + 1]
        a = amounts[idx - lookback:idx + 1]

        dt = dates[idx]
        cl = closes[idx]
        w = len(c)

        fv = {}
        # 基础因子 (use numpy for speed)
        if "momentum" in factor_names:
            n_mom = min(20, w)
            fv["momentum"] = (c[-1] - c[-n_mom - 1]) / c[-n_mom - 1] if w > n_mom and c[-n_mom - 1] > 0 else None

        if "volatility" in factor_names:
            n_vol = min(20, len(r))
            ret_std = np.std(r[-n_vol:]) if len(r) >= n_vol else None
            fv["volatility"] = float(ret_std * np.sqrt(252)) if ret_std is not None else None

        if "vol_ratio" in factor_names:
            v5 = np.mean(v[-5:]) if len(v) >= 5 else None
            v20 = np.mean(v[-min(20, w):]) if w >= 5 else None
            fv["vol_ratio"] = float(v5 / v20) if v5 and v20 and v20 > 0 else None

        if "sharpe" in factor_names:
            n_sh = min(60, len(r))
            if len(r) >= n_sh and np.std(r[-n_sh:]) > 0:
                fv["sharpe"] = float(np.mean(r[-n_sh:]) / np.std(r[-n_sh:]) * np.sqrt(252))
            else:
                fv["sharpe"] = None

        if "pv_corr" in factor_names:
            n_c = min(20, w)
            if n_c > 5:
                cc = _safe_corrcoef(c[-n_c:], v[-n_c:])
                fv["pv_corr"] = float(cc) if not np.isnan(cc) else None
            else:
                fv["pv_corr"] = None

        if "max_dd" in factor_names:
            n_dd = min(60, w)
            peak = np.maximum.accumulate(c[-n_dd:])
            dd = (c[-n_dd:] - peak) / np.maximum(peak, 1e-10)
            fv["max_dd"] = float(np.min(dd))

        if "up_ratio" in factor_names:
            n_u = min(20, len(r))
            up = np.sum(r[-n_u:] > 0)
            down = np.sum(r[-n_u:] < 0)
            fv["up_ratio"] = float(up / down) if down > 0 else (float(up) if up > 0 else 1.0)

        if "chg_std" in factor_names:
            n_cs = min(20, len(r))
            std_r = np.std(r[-n_cs:]) if len(r) >= n_cs else None
            fv["chg_std"] = float(std_r) if std_r is not None else None

        # Alpha101 因子
        if "alpha006" in factor_names:
            n6 = min(10, w)
            if n6 > 3:
                cc = _safe_corrcoef(o[-n6:], v[-n6:])
                fv["alpha006"] = float(-cc) if not np.isnan(cc) else None
            else:
                fv["alpha006"] = None

        if "alpha009" in factor_names:
            d1_vals = np.diff(c[-6:]) if len(c) >= 6 else np.array([])
            if len(d1_vals) >= 5:
                tmin = np.min(d1_vals[-5:])
                tmax = np.max(d1_vals[-5:])
                d1 = d1_vals[-1]
                if tmin > 0: fv["alpha009"] = float(d1)
                elif tmax < 0: fv["alpha009"] = float(d1)
                else: fv["alpha009"] = float(-d1)
            else:
                fv["alpha009"] = None

        if "alpha012" in factor_names:
            if idx >= 2:
                dv = volumes[idx] - volumes[idx - 1]
                dc = closes[idx] - closes[idx - 1]
                fv["alpha012"] = float(np.sign(dv) * (-dc)) if dv != 0 else 0.0
            else:
                fv["alpha012"] = None

        if "alpha032" in factor_names:
            n32 = min(20, w)
            sm7 = np.sum(c[-7:]) if w >= 7 else 0
            mean_rev = (sm7 / 7.0 - c[-1]) if sm7 else 0
            # vwap from amount/volume
            vwap_arr = _vwap_array(a, v, h, lo, c)
            if w >= 6 and n32 > 5:
                delayed = np.roll(c, 5)[-n32:]
                cc = _safe_corrcoef(vwap_arr[-n32:], delayed)
                fv["alpha032"] = float(mean_rev + (cc if not np.isnan(cc) else 0))
            else:
                fv["alpha032"] = mean_rev

        if "alpha034" in factor_names:
            if len(r) >= 20 and c[-2] > 0:
                ratios = []
                for j in range(5, len(r) + 1):
                    std5 = np.std(r[j - 5:j])
                    std2 = np.std(r[j - 2:j])
                    ratios.append(std2 / std5 if std5 > 0 else 0.0)
                cur_ratio = ratios[-1]
                d1_series = r[-20:]
                d1 = (c[-1] - c[-2]) / c[-2]
                fv["alpha034"] = float((1 - _percent_rank(ratios[-20:], cur_ratio)) + (1 - _percent_rank(d1_series, d1)))
            else:
                fv["alpha034"] = None

        if "alpha038" in factor_names:
            n38 = min(10, w)
            if n38 > 3:
                # ts_rank: position in sorted window
                tr = (np.searchsorted(np.sort(c[-n38:]), c[-1])) / (n38 - 1) if n38 > 1 else 0.5
                cc = _safe_corrcoef(c[-n38:], v[-n38:])
                fv["alpha038"] = float(-tr * cc) if not np.isnan(cc) else None
            else:
                fv["alpha038"] = None

        if "alpha042" in factor_names:
            n42 = min(20, w)
            if n42 > 5:
                vwap_arr = _vwap_array(a, v, h, lo, c)
                spread = vwap_arr - c
                level = vwap_arr + c
                spread_rank = _percent_rank(spread[-n42:], spread[-1])
                level_rank = _percent_rank(level[-n42:], level[-1])
                fv["alpha042"] = float(spread_rank / max(level_rank, 1e-6))
            else:
                fv["alpha042"] = None

        if "alpha046" in factor_names:
            if w > 20:
                d20 = c[-1] - c[-21]
                d20_pct = d20 / c[-21] if c[-21] > 0 else 0
                if d20_pct > 0.05:
                    fv["alpha046"] = float(-np.std(c[-5:]))
                else:
                    fv["alpha046"] = 1.0
            else:
                fv["alpha046"] = None

        if "alpha054" in factor_names:
            if h[-1] != lo[-1] and c[-1] != 0:
                num = -1 * (lo[-1] - c[-1]) * (o[-1] ** 5)
                den = (lo[-1] - h[-1]) * (c[-1] ** 5)
                fv["alpha054"] = float(num / den) if den != 0 else 0.0
            else:
                fv["alpha054"] = None

        if "alpha057" in factor_names:
            n57 = min(20, w)
            ma20 = np.mean(c[-n57:])
            fv["alpha057"] = float(ma20 / c[-1]) if c[-1] > 0 else None

        if "alpha065" in factor_names:
            n65 = min(15, w)
            # adv20 SMA of volume
            adv = np.convolve(v, np.ones(20)/20, mode='same')
            if n65 > 3 and len(adv) >= n65:
                c15 = _safe_corrcoef(c[-n65:], adv[-n65:])
                n5 = min(5, w)
                c5 = _safe_corrcoef(c[-n5:], adv[-n5:]) if n5 > 3 else 0
                fv["alpha065"] = 1.0 if (not np.isnan(c15) and not np.isnan(c5) and c15 < c5) else 0.0
            else:
                fv["alpha065"] = None

        if "alpha081" in factor_names:
            n81 = min(10, w)
            vwap_arr = _vwap_array(a, v, h, lo, c)
            adv = np.convolve(v, np.ones(20)/20, mode='same')
            if n81 > 3 and len(adv) >= n81:
                cc = _safe_corrcoef(vwap_arr[-n81:], adv[-n81:])
                fv["alpha081"] = float(np.log(max(abs(cc), 1e-6))) if cc and not np.isnan(cc) else None
            else:
                fv["alpha081"] = None

        if "alpha101" in factor_names:
            fv["alpha101"] = float((c[-1] - o[-1]) / (h[-1] - lo[-1] + 0.001))

        # 去 None 的条目
        for fn in factor_names:
            v = fv.get(fn)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                result[fn].append({"date": dt, "value": v, "close": cl})

    return result

    # 4. 逐日回测
    equity = 1.0
    equity_curve = []
    prev_top_codes = set()
    prev_close = {}
    entry_info = {}
    trades = []
    day_idx = 0

    for dt in all_dates:
        scores = []
        day_prices = {}
        for code, fs in factor_series.items():
            di = date_index[code].get(dt)
            if di is None:
                continue
            p = fs[di]
            scores.append((code, p["value"]))
            day_prices[code] = p["close"]

        if len(scores) < top_k:
            continue

        scores.sort(key=lambda x: x[1], reverse=higher_better)
        top_codes = set(c for c, _ in scores[:top_k])

        # 当日收益
        daily_ret = 0
        ret_count = 0
        for code in top_codes:
            p_close = day_prices.get(code)
            if not p_close:
                continue
            pc = prev_close.get(code)
            if pc and pc > 0:
                daily_ret += (p_close / pc - 1)
                ret_count += 1
        if ret_count > 0:
            daily_ret /= ret_count
            equity *= (1 + daily_ret)

        # 调仓
        bought = top_codes - prev_top_codes
        sold = prev_top_codes - top_codes
        for code in sold:
            h = entry_info.pop(code, None)
            sell_price = day_prices.get(code)
            if h and h.get("entry_price") and sell_price:
                pnl_pct = (sell_price / h["entry_price"] - 1) * 100
                trades.append({
                    "code": code, "buy_date": h["entry_date"], "sell_date": dt,
                    "buy_price": round(h["entry_price"], 2),
                    "sell_price": round(sell_price, 2),
                    "profit_pct": round(pnl_pct, 2),
                    "hold_days": day_idx - h["entry_idx"],
                })
        for code in bought:
            entry_info[code] = {
                "entry_date": dt,
                "entry_price": day_prices.get(code),
                "entry_idx": day_idx,
            }

        for code in top_codes:
            if code in day_prices:
                prev_close[code] = day_prices[code]

        prev_top_codes = top_codes
        day_idx += 1

        equity_curve.append({
            "date": dt, "equity": round(equity, 4),
            "return": round((equity - 1) * 100, 2),
        })

    # 清仓
    if equity_curve and prev_top_codes:
        last_date = equity_curve[-1]["date"]
        for code, h in entry_info.items():
            sell_price = prev_close.get(code)
            if sell_price and h.get("entry_price"):
                pnl_pct = (sell_price / h["entry_price"] - 1) * 100
                trades.append({
                    "code": code, "buy_date": h["entry_date"], "sell_date": last_date,
                    "buy_price": round(h["entry_price"], 2),
                    "sell_price": round(sell_price, 2),
                    "profit_pct": round(pnl_pct, 2),
                    "hold_days": day_idx - h["entry_idx"],
                })

    return equity_curve, trades


@app.route("/api/lab/backtest", methods=["POST"])
def api_lab_backtest():
    """因子全量回测：8个因子各自生成收益曲线 + 交易详情"""
    payload = request.get_json(silent=True) or {}
    source = payload.get("source", "all")  # 'etf' | 'concept' | 'all'
    start_date = payload.get("start_date", "")
    end_date = payload.get("end_date", "")
    top_k = min(payload.get("top_k", 5), 10)
    selected_factors = payload.get("factors")
    selected_factor = payload.get("factor", "all")

    # 读取所有数据 + 一次性预计算全部因子时序
    items_data = {}
    item_names = {}
    if isinstance(selected_factors, list):
        if "all" in selected_factors:
            all_factors = list(FACTOR_META.keys())
        else:
            all_factors = [f for f in selected_factors if f in FACTOR_META]
            invalid_factors = [f for f in selected_factors if f not in FACTOR_META]
            if invalid_factors:
                return jsonify({"error": "因子不存在"}), 400
            if not all_factors:
                return jsonify({"error": "请选择至少一个因子"}), 400
    elif selected_factor and selected_factor != "all":
        if selected_factor not in FACTOR_META:
            return jsonify({"error": "因子不存在"}), 400
        all_factors = [selected_factor]
    else:
        all_factors = list(FACTOR_META.keys())

    cache_params = {
        "version": LAB_CACHE_VERSION,
        "source": source,
        "start_date": start_date,
        "end_date": end_date,
        "top_k": top_k,
        "factors": all_factors,
        "data": _lab_data_signature(source),
    }
    cached, cache_key = _load_lab_backtest_cache(cache_params)
    if cached is not None:
        return jsonify(cached)

    # 根据 source 加载全部标的。放在缓存命中之后，避免命中缓存时读取全量 CSV。
    items = []
    if source in ("etf", "all"):
        items.extend([{**i, "type": "etf"} for i in _list_etfs_from_csv()])
    if source in ("concept", "all"):
        items.extend([{**i, "type": "concept"} for i in _list_concepts_from_csv()])

    if not items:
        return jsonify({"error": "没有找到标的"}), 400
    if len(items) < top_k:
        top_k = max(1, len(items) // 2)

    for item in items:
        code = item["code"]
        item_names[code] = item.get("name", code)
        if item.get("type") == "etf":
            rows = _read_etf_daily(code, start_date="2014-01-01", end_date=end_date)
        else:
            rows = _read_concept_daily(code, start_date="2014-01-01", end_date=end_date)
        if rows and len(rows) >= 60:
            items_data[code] = rows

    # 如果标的太多，取成交量最大的 top 80
    if len(items_data) > 80:
        vol_rank = []
        for code, rows in items_data.items():
            recent_vol = sum(r["volume"] or 0 for r in rows[-20:]) / 20 if len(rows) >= 20 else 0
            vol_rank.append((code, recent_vol))
        vol_rank.sort(key=lambda x: x[1], reverse=True)
        keep_codes = set(c for c, _ in vol_rank[:80])
        items_data = {c: items_data[c] for c in keep_codes}

    if len(items_data) < 2:
        return jsonify({"error": "有效标的不够"}), 400

    logger.info("GTJA pre-computing %d factors for %d items...", len(all_factors), len(items_data))
    gtja_series = precompute_gtja_factor_series(items_data, all_factors, start_date=start_date, lookback=260)

    factor_results = {}
    for fn in all_factors:
        fn_series = gtja_series.get(fn, {})
        if len(fn_series) < top_k:
            factor_results[fn] = None
            continue
        equity, trades = _run_factor_backtest_cached(fn_series, fn, start_date, end_date, top_k)
        if not equity:
            factor_results[fn] = None
            continue

        # stats
        win_trades = [t for t in trades if t["profit_pct"] > 0]
        loss_trades = [t for t in trades if t["profit_pct"] <= 0]
        avg_win = sum(t["profit_pct"] for t in win_trades) / max(1, len(win_trades))
        avg_loss = sum(t["profit_pct"] for t in loss_trades) / max(1, len(loss_trades))

        # 日收益率序列
        daily_rets = []
        for i in range(1, len(equity)):
            if equity[i - 1]["equity"] > 0:
                daily_rets.append(equity[i]["equity"] / equity[i - 1]["equity"] - 1)
        import math
        avg_ret = sum(daily_rets) / max(1, len(daily_rets))
        std_ret = (sum((r - avg_ret) ** 2 for r in daily_rets) / max(1, len(daily_rets))) ** 0.5
        sharpe = (avg_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

        # 最大回撤
        peak = 0
        max_dd = 0
        for p in equity:
            if p["equity"] > peak:
                peak = p["equity"]
            dd = (p["equity"] - peak) / peak if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd

        total_return = equity[-1]["return"] if equity else 0

        factor_results[fn] = {
            "equity_curve": equity,
            "trades": trades,
            "stats": {
                "total_return": round(total_return, 2),
                "sharpe": round(sharpe, 2),
                "max_dd": round(max_dd * 100, 1),
                "trade_count": len(trades),
                "win_rate": round(len(win_trades) / max(1, len(trades)) * 100, 0),
                "avg_win": round(avg_win, 1),
                "avg_loss": round(avg_loss, 1),
            },
        }

    response = {
        "factors": factor_results,
        "factor_meta": FACTOR_META,
        "requested_factors": all_factors,
        "available_factors": [fn for fn, value in factor_results.items() if value],
        "top_k": top_k,
        "item_names": item_names,
        "item_count": len(items_data),
        "window": f"{start_date} ~ {end_date}",
    }
    response = _save_lab_backtest_cache(cache_params, response)
    response["cache"]["key"] = cache_key
    return jsonify(response)


# ── stats ──────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    with get_session() as sess:
        stock_count = sess.query(func.count(StockInfo.code)).filter(StockInfo.type == "1", StockInfo.status == 1).scalar()
        daily_count = sess.query(func.count(StockDaily.id)).scalar()
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()

    # concept count from CSV
    concept_count = len(_list_concepts_from_csv())

    return jsonify({
        "stocks": stock_count,
        "daily_records": daily_count,
        "concepts": concept_count,
        "stock_concept_relations": 0,
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
