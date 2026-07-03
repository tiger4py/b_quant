import json
import logging
import os
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

@app.route("/api/trading/journal", methods=["GET"])
def api_trading_journal_list():
    """获取操作心得列表，可按日期筛选"""
    date_filter = request.args.get("date", "")
    entries = _load_journal()
    if date_filter:
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
    # 同一天覆盖更新
    existing = next((e for e in entries if e.get("date") == date), None)
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
        "market_view": (payload.get("market_view") or "").strip(),
        "sector_view": (payload.get("sector_view") or "").strip(),
        "trade_rationale": (payload.get("trade_rationale") or "").strip(),
        "lessons": (payload.get("lessons") or "").strip(),
        "tomorrow_plan": (payload.get("tomorrow_plan") or "").strip(),
        "review": {
            "verdict": review.get("verdict") or "",      # "对" / "错" / "半对" / ""
            "note": (review.get("note") or "").strip(),   # 复盘笔记
        },
        "stocks": stocks,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if existing:
        existing.update(entry)
    else:
        entry["id"] = max((e.get("id", 0) for e in entries), default=0) + 1
        entries.append(entry)

    _save_journal(entries)
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
