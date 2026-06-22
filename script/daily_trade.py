#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日交易台账 — 基于 strategy_trend_following 的买卖操作建议

流程：
  1. 读取持仓文件 (portfolio.json)
  2. 对每只持仓股跑 generate_signals → 检查今日是否有卖出信号
  3. 全市场扫描今日买入信号
  4. 扣除已持仓、扣除卖出释放的仓位 → 剩余仓位填满（最多5只）
  5. 输出：今日卖出 / 今日买入 / 操作清单

用法：
  python script/daily_trade.py                          # 标准模式
  python script/daily_trade.py --portfolio my_port.json  # 指定持仓文件

持仓文件格式 (portfolio.json):
{
  "cash": 400000,
  "max_positions": 5,
  "holdings": [
    {"code": "sz.300967", "name": "晓鸣股份", "shares": 7900, "buy_price": 15.80, "buy_date": "2026-06-10"},
    {"code": "bj.836807", "name": "奔朗新材", "shares": 2000, "buy_price": 6.80, "buy_date": "2026-06-12", "note": "北交所"},
    {"code": "sz.301509", "name": "金凯生科", "shares": 4800, "buy_price": 26.50, "buy_date": "2026-06-15"}
  ]
}
"""
import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo, Concept, StockConcept
from backtest.strategy.strategy_trend_following import generate_signals

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============ 默认持仓文件 ============
DEFAULT_PORTFOLIO = ROOT_DIR / "data" / "portfolio.json"


def load_portfolio(path):
    """加载持仓文件。"""
    if not os.path.exists(path):
        print(f"[!] 持仓文件不存在: {path}")
        print("    使用默认空持仓")
        return {"cash": 400000, "max_positions": 5, "holdings": []}

    with open(path, "r", encoding="utf-8") as f:
        p = json.load(f)

    p.setdefault("cash", 0)
    p.setdefault("max_positions", 5)
    p.setdefault("holdings", [])
    # 过滤掉标记为无法处理的持仓
    p["active_holdings"] = [h for h in p["holdings"] if not h.get("skip", False)]
    return p


def save_portfolio(path, portfolio):
    """保存持仓文件。"""
    portfolio.pop("active_holdings", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
    print(f"\n[↓] 持仓已更新: {path}")


def _weekday(date_str):
    """日期 + 星期。"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{date_str} ({wd[dt.weekday()]})"
    except Exception:
        return date_str


# ============ Phase 1: 检查持仓卖出信号 ============

def check_sells(sess, holdings, latest_date, load_bars):
    """检查每只持仓股在最新日期是否有卖出信号。"""
    sells = []
    keeps = []

    for h in holdings:
        code = h["code"]

        # 北交所等无法处理的
        if h.get("note"):
            print(f"  [!] {code} {h['name']}: {h['note']}，跳过")
            keeps.append(h)
            continue

        bars = load_bars(sess, code)
        if not bars or len(bars) < 45:
            print(f"  [!] {code} {h['name']}: K线不足45根，跳过")
            keeps.append(h)
            continue

        # 确保最新日期在数据中
        last_bar_date = bars[-1]["trade_date"]
        if last_bar_date != latest_date:
            print(f"  [!] {code} {h['name']}: 最新数据={last_bar_date}，不是{latest_date}，跳过")
            keeps.append(h)
            continue

        signals = generate_signals(bars)
        for s in signals:
            if s["date"] == latest_date and s["action"] == "sell":
                current_price = bars[-1]["close"]
                profit = (current_price / h["buy_price"] - 1) * 100
                sells.append({
                    **h,
                    "sell_reason": s["reason"],
                    "current_price": current_price,
                    "profit_pct": round(profit, 2),
                })
                print(f"  [!!] {code} {h['name']} 触发卖出: {s['reason']}")
                print(f"       买入价 {h['buy_price']:.2f} → 现价 {current_price:.2f} ({profit:+.1f}%)")
                break
        else:
            # 没有卖出信号 → 继续持有
            current_price = bars[-1]["close"]
            profit = (current_price / h["buy_price"] - 1) * 100
            keeps.append(h)
            print(f"  [OK] {code} {h['name']}: 继续持有 | 现价 {current_price:.2f} ({profit:+.1f}%)")

    return sells, keeps


# ============ Phase 2: 全市场买入信号（无状态扫描）============

# 趋势跟随策略买入条件（与 strategy_trend_following.py 保持同步）
from backtest.strategy.strategy_trend_following import (
    PRICE_UP_5D_MIN, PRICE_UP_20D_MIN, PRICE_DOWN_40D_MAX,
    CLOSE_ABOVE_MA20, PRICE_NEAR_20D_HIGH, PRICE_ABOVE_MA20_MAX,
    VOL_RATIO_BUY, VOL_RATIO_MAX, VOL_TREND_ACCEL,
    UP_VOL_RATIO, LOOKBACK_DAYS, MIN_CONSEC_UP,
)


def _check_buy_today(bars):
    """
    无状态检测：最新一根K线是否满足趋势跟随策略的所有买入条件。

    与 generate_signals 不同，此函数不做持仓跟踪，
    纯粹判断「今天这个点位该不该买」。

    返回: dict（含信号详情+评分） 或 None（不满足条件）
    """
    from backtest.indicators import sma

    if len(bars) < 45:
        return None

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    idx = len(closes) - 1  # 最新一根
    close = closes[idx]

    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    if ma10[idx] is None or ma20[idx] is None:
        return None
    if vol_ma5[idx] is None or vol_ma20[idx] is None or vol_ma20[idx] == 0:
        return None

    vol_ratio = vol_ma5[idx] / vol_ma20[idx]

    # ---- 趋势过滤 ----
    if closes[idx - 5] <= 0:
        return None
    chg_5d = (close - closes[idx - 5]) / closes[idx - 5]
    if chg_5d < PRICE_UP_5D_MIN:
        return None

    chg_20d = (close - closes[idx - 20]) / closes[idx - 20] if closes[idx - 20] > 0 else 0
    if chg_20d < PRICE_UP_20D_MIN:
        return None

    # 40日最大回撤（滚动峰值法）
    peak_40 = closes[idx - 39]
    max_dd_40 = 0.0
    for j in range(idx - 38, idx + 1):
        if closes[j] > peak_40:
            peak_40 = closes[j]
        dd = (closes[j] - peak_40) / peak_40
        if dd < max_dd_40:
            max_dd_40 = dd
    if max_dd_40 < PRICE_DOWN_40D_MAX:
        return None

    # ---- 位置过滤 ----
    if close <= ma10[idx] or close <= ma20[idx]:
        return None

    high_20 = max(highs[idx - 19:idx + 1])
    if (high_20 - close) / high_20 > PRICE_NEAR_20D_HIGH:
        return None

    if close > ma20[idx] * PRICE_ABOVE_MA20_MAX:
        return None

    # ---- 量能过滤 ----
    if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
        return None

    # 量能加速
    if idx >= 6:
        recent_3 = sum(volumes[idx - 2:idx + 1]) / 3
        prior_3 = sum(volumes[idx - 5:idx - 2]) / 3
        if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
            return None

    # ---- 量价配合 ----
    from backtest.strategy.strategy_trend_following import _compute_price_volume_dynamics
    dyn = _compute_price_volume_dynamics(closes, volumes, idx)
    if dyn["up_vol_ratio"] < UP_VOL_RATIO:
        return None
    if dyn["consecutive_up"] < MIN_CONSEC_UP:
        return None

    # ---- 评分（对齐趋势跟随策略核心维度） ----
    chg_10d = (close - closes[idx - 10]) / closes[idx - 10] if idx >= 10 and closes[idx - 10] > 0 else 0
    dist_ma20 = (close - ma20[idx]) / ma20[idx]

    # 趋势强度 (30分)
    score_trend = min(30, max(0, chg_5d * 100 * 2 + (chg_20d > 0) * 10))

    # 量能质量 (25分)
    score_vol = min(25, max(0, (vol_ratio - 1.3) / 2.7 * 25))

    # 量价配合 (25分)
    score_coord = min(25, max(0, (dyn["up_vol_ratio"] - 1.1) / 1.4 * 25))

    # 位置合理性 (20分)
    score_pos = 20
    if dist_ma20 < 0.01: score_pos -= 5       # 贴MA20太近
    elif dist_ma20 > 0.10: score_pos -= 10    # 离MA20太远
    if dyn["consecutive_up"] < 2: score_pos -= 5

    total_score = score_trend + score_vol + score_coord + score_pos

    return {
        "code": "", "name": "",
        "close": close, "ma20": round(ma20[idx], 2),
        "chg_5d": round(chg_5d * 100, 1),
        "chg_10d": round(chg_10d * 100, 1),
        "chg_20d": round(chg_20d * 100, 1),
        "vol_ratio": round(vol_ratio, 1),
        "up_vol_ratio": round(dyn["up_vol_ratio"], 1),
        "consecutive_up": dyn["consecutive_up"],
        "dist_ma20": round(dist_ma20 * 100, 1),
        "daily_chg": round((closes[idx] - closes[idx - 1]) / closes[idx - 1] * 100, 1) if idx > 0 else 0,
        "score": round(total_score, 1),
        "scores": {
            "trend": round(score_trend, 1),
            "volume": round(score_vol, 1),
            "coord": round(score_coord, 1),
            "position": round(score_pos, 1),
        },
        "reason": (
            f"趋势跟随(5日{chg_5d*100:.1f}% 20日{chg_20d*100:.1f}% | "
            f"量比{vol_ratio:.1f}x 涨跌量比{dyn['up_vol_ratio']:.1f}x | "
            f"连涨{dyn['consecutive_up']}天)"
        ),
    }


def find_buys(sess, latest_date, held_codes, load_market_bars):
    """全市场无状态扫描今日买入信号，排除已持仓的。"""
    print(f"\n[2/3] 全市场扫描买入信号（无状态检测）...")

    bars_by_code = load_market_bars(sess)

    buy_signals = []
    for code, bars in bars_by_code.items():
        if len(bars) < 45:
            continue
        if bars[-1]["trade_date"] != latest_date:
            continue
        if code in held_codes:
            continue

        result = _check_buy_today(bars)
        if result is None:
            continue

        result["code"] = code
        buy_signals.append(result)

    print(f"  扫描到 {len(buy_signals)} 只买入候选（已排除已持仓）")

    # 查股票名称
    if buy_signals:
        stock_rows = sess.query(StockInfo.code, StockInfo.name).filter(
            StockInfo.code.in_([s["code"] for s in buy_signals])
        ).all()
        name_map = {r.code: r.name for r in stock_rows}
        for s in buy_signals:
            s["name"] = name_map.get(s["code"], s["code"])

    # 按评分排序
    buy_signals.sort(key=lambda s: s["score"], reverse=True)
    return buy_signals


# ============ Phase 3: 生成操作清单 ============

def generate_plan(portfolio, sells, keeps, buy_signals, latest_date):
    """生成最终买卖计划。"""
    print(f"\n{'=' * 60}")
    print(f"  交易计划 — {_weekday(latest_date)}")
    print(f"{'=' * 60}")

    # 统计仓位
    max_pos = portfolio["max_positions"]
    held_after_sells = [k for k in keeps if not k.get("skip")]
    available_slots = max_pos - len(held_after_sells)
    cash = portfolio["cash"]

    # 加上卖出释放的现金
    sell_cash = 0
    for s in sells:
        sell_cash += s["shares"] * s["current_price"]

    # ---- 卖出清单 ----
    print(f"\n  ┌─ 今日卖出 ({len(sells)} 只) ──────────────────────")
    if sells:
        for s in sells:
            amount = s["shares"] * s["current_price"]
            print(f"  │ [卖] {s['code']} {s['name']}")
            print(f"  │      {s['shares']}股 × {s['current_price']:.2f} = {amount:,.0f}元")
            print(f"  │      买入价 {s['buy_price']:.2f} | 盈亏 {s['profit_pct']:+.1f}%")
            print(f"  │      原因: {s['sell_reason']}")
            print(f"  │")
        print(f"  │ 释放资金: {sell_cash:,.0f} 元")
    else:
        print(f"  │ (无)")
    print(f"  └──────────────────────────────────────")

    # ---- 持仓保持 ----
    print(f"\n  ┌─ 继续持有 ({len(keeps)} 只) ────────────────────")
    for k in keeps:
        note = f" — {k.get('note', '')}" if k.get("note") else ""
        print(f"  │ {k['code']} {k['name']}{note}")
    print(f"  └──────────────────────────────────────")

    # ---- 买入清单 ----
    print(f"\n  ┌─ 今日买入候选 (仓位余 {available_slots} 只) ──────────")
    if available_slots <= 0:
        print(f"  │ 仓位已满 ({len(held_after_sells)}/{max_pos})，今日不买")
    elif not buy_signals:
        print(f"  │ 无符合条件的买入信号")
    else:
        # 每只分配仓位资金
        total_cash = cash + sell_cash
        per_slot = total_cash / max(1, available_slots) if total_cash > 0 else 0

        # 先列出所有候选（不管资金）
        print(f"  │ 可用资金: {total_cash:,.0f} | 单只上限: {per_slot:,.0f}")
        print(f"  │")
        shown = 0
        for s in buy_signals[:available_slots + 3]:  # 多展示几只备选
            if shown >= available_slots + 2:
                break
            shares = int(per_slot // s["close"] // 100 * 100) if per_slot > 0 else 0
            amount = shares * s["close"] if shares > 0 else 0
            can_buy = "✓" if shares >= 100 else "✗资金不足"

            print(f"  │ [{can_buy}] {s['code']} {s['name']} — 评分{s['score']:.0f}")
            print(f"  │     {s['close']:.2f}元 | 5日{s['chg_5d']:+.1f}% | 20日{s['chg_20d']:+.1f}% | 量比{s['vol_ratio']:.1f}x")
            print(f"  │     趋势{s['scores']['trend']:.0f} 量能{s['scores']['volume']:.0f} 配合{s['scores']['coord']:.0f} 位置{s['scores']['position']:.0f}")
            if shares >= 100:
                print(f"  │     → {shares}股 × {s['close']:.2f} = {amount:,.0f}元")
            print(f"  │     → {s['reason']}")
            print(f"  │")
            shown += 1
    print(f"  └──────────────────────────────────────")

    # ---- 总结 ----
    print(f"\n  ══════════════════════════════════════")
    print(f"  持仓: {len(portfolio['holdings'])} → 操作后 {len(keeps)} 保持")
    if sells:
        print(f"  ⚠️ 请先执行卖出，释放资金后再买入")
    if cash + sell_cash <= 0:
        print(f"  ⚠️ 当前无可用现金，需先卖出或入金才能买入")


# ============ 数据加载辅助 ============

def _load_bars_for_code(sess, code):
    """加载单只股票最近200天K线。"""
    rows = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
        .order_by(StockDaily.trade_date.desc())
        .limit(200)
        .all()
    )
    rows.reverse()
    return [
        {
            "trade_date": r.trade_date,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "amount": r.amount,
        }
        for r in rows
    ]


def _load_market_bars(sess):
    """加载全市场最近200天K线。"""
    date_rows = (
        sess.query(StockDaily.trade_date)
        .distinct()
        .order_by(desc(StockDaily.trade_date))
        .limit(200)
        .all()
    )
    if not date_rows:
        return {}
    cutoff = min(r[0] for r in date_rows)

    rows = (
        sess.query(StockDaily)
        .join(StockInfo, StockDaily.code == StockInfo.code)
        .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date >= cutoff)
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )

    bars_by_code = defaultdict(list)
    for r in rows:
        bars_by_code[r.code].append({
            "trade_date": r.trade_date,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "amount": r.amount,
        })
    return bars_by_code


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser(description="每日交易台账 — 基于趋势跟随策略")
    parser.add_argument("--portfolio", type=str, default=str(DEFAULT_PORTFOLIO), help="持仓文件路径")
    parser.add_argument("--max-positions", type=int, default=5, help="最大持仓数")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        # 最新日期
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
        print(f"数据日期: {_weekday(latest_date)}")

        # 加载持仓
        portfolio = load_portfolio(args.portfolio)
        portfolio["max_positions"] = args.max_positions
        holdings = portfolio.get("active_holdings", portfolio["holdings"])

        if not holdings:
            print("当前无持仓")

        print(f"\n{'=' * 60}")
        print(f"  当前持仓: {len(holdings)} 只 | 现金: {portfolio['cash']:,.0f} | 上限: {args.max_positions} 仓")
        print(f"{'=' * 60}")

        # ---- Phase 1: 检查持仓卖出 ----
        print(f"\n[1/3] 检查持仓卖出信号...")
        sells, keeps = check_sells(sess, holdings, latest_date, _load_bars_for_code)

        # ---- Phase 2: 全市场买入 ----
        held_codes = {h["code"] for h in keeps if not h.get("note")}
        buy_signals = find_buys(sess, latest_date, held_codes, _load_market_bars)

        # ---- Phase 3: 生成计划 ----
        generate_plan(portfolio, sells, keeps, buy_signals, latest_date)

        # ---- Phase 4: QQ推送 ----
        qq_msg = _build_qq_message(portfolio, sells, keeps, buy_signals, latest_date)
        _push_qq(qq_msg)

        # ---- Phase 5: 记录历史 ----
        _save_trade_history(latest_date, sells, keeps, buy_signals, portfolio)

        # ---- 更新持仓文件 ----
        new_holdings = keeps
        updated = {**portfolio, "holdings": new_holdings}
        updated.pop("active_holdings", None)
        save_portfolio(args.portfolio, updated)

    finally:
        sess.close()

    print("\nDone.")


# ============ QQ推送 ============

def _build_qq_message(portfolio, sells, keeps, buy_signals, latest_date):
    """构建QQ推送消息。"""
    lines = [
        f"[*] 趋势跟随 — {_weekday(latest_date)}",
        f"持仓 {len(keeps)} 只 | 可用 {portfolio['cash']:,.0f} | 上限 {portfolio['max_positions']} 仓",
        "",
    ]

    # 卖出
    if sells:
        lines.append("--- 今日卖出 ---")
        for s in sells:
            lines.append(f"[卖] {s['name']}({s['code']}) {s['profit_pct']:+.1f}% → {s['sell_reason']}")
    else:
        lines.append("[卖] 无")

    # 持有
    if keeps:
        lines.append("")
        lines.append("--- 继续持有 ---")
        for k in keeps:
            note = f" [{k.get('note')}]" if k.get('note') else ""
            lines.append(f"  {k['name']}({k['code']}){note}")

    # 买入候选（前5只）
    total_cash = portfolio["cash"] + sum(s["shares"] * s["current_price"] for s in sells)
    active_keeps = [k for k in keeps if not k.get("note")]
    slots = portfolio["max_positions"] - len(active_keeps)
    per_slot = total_cash / max(1, slots) if total_cash > 0 else 0

    if slots > 0 and buy_signals:
        lines.append("")
        lines.append(f"--- 买入候选 (余{slots}仓 | 单只{per_slot:,.0f}) ---")
        count = 0
        for s in buy_signals[:slots + 2]:
            if count >= slots:
                break
            shares = int(per_slot // s["close"] // 100 * 100) if per_slot > 0 else 0
            if shares < 100:
                continue
            amount = shares * s["close"]
            lines.append(
                f"{count+1}. {s['name']}({s['code']}) 评分{s['score']:.0f} "
                f"{shares}股×{s['close']:.2f}={amount:,.0f}"
            )
            lines.append(
                f"   5日{s['chg_5d']:+.1f}% 量比{s['vol_ratio']:.1f}x "
                f"涨跌量比{s['up_vol_ratio']:.1f}x 连涨{s['consecutive_up']}天"
            )
            count += 1
        if count == 0:
            lines.append("  资金不足，无法买入")
    elif slots <= 0:
        lines.append("")
        lines.append("[买] 仓位已满")

    lines.append("")
    lines.append("--- 趋势跟随 · 仅供参考 ---")
    return "\n".join(lines)


# ============ 交易历史记录 ============

HISTORY_FILE = ROOT_DIR / "data" / "trade_history.json"


def _save_trade_history(date, sells, keeps, buy_signals, portfolio):
    """追加今日交易计划到历史记录。"""
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    # 去重：同一天不重复记录
    if history and history[-1].get("date") == date:
        history.pop()

    # 计算当前持仓市值
    holding_value = 0
    for k in keeps:
        holding_value += k.get("shares", 0) * k.get("current_price", k.get("buy_price", 0))

    record = {
        "date": date,
        "cash": portfolio["cash"],
        "holding_value": round(holding_value, 2),
        "total_value": round(portfolio["cash"] + holding_value, 2),
        "sells": [
            {
                "code": s["code"],
                "name": s["name"],
                "shares": s.get("shares", 0),
                "buy_price": s.get("buy_price", 0),
                "sell_price": s.get("current_price", 0),
                "profit_pct": s.get("profit_pct", 0),
                "reason": s.get("sell_reason", ""),
            }
            for s in sells
        ],
        "keeps": [
            {
                "code": k["code"],
                "name": k["name"],
                "shares": k.get("shares", 0),
                "buy_price": k.get("buy_price", 0),
                "current_price": k.get("current_price", k.get("buy_price", 0)),
                "profit_pct": round((k.get("current_price", k.get("buy_price", 0)) / k.get("buy_price", 1) - 1) * 100, 2),
            }
            for k in keeps
        ],
        "buy_signals": [
            {
                "code": s["code"],
                "name": s["name"],
                "close": s["close"],
                "chg_5d": s["chg_5d"],
                "chg_20d": s["chg_20d"],
                "vol_ratio": s["vol_ratio"],
                "up_vol_ratio": s.get("up_vol_ratio", 0),
                "consecutive_up": s.get("consecutive_up", 0),
                "score": s.get("score", 0),
                "reason": s["reason"],
            }
            for s in buy_signals[:5]
        ],
        "position_count": len(keeps),
    }

    history.append(record)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"\n[↓] 交易记录已保存: {HISTORY_FILE}")


def _push_qq(msg):
    """推送到QQ。"""
    try:
        from models.qq_webhook import QQPusher
        pusher = QQPusher()
        if pusher.enabled:
            result = pusher.push_long_text(msg)
            print(f"\n[QQ] 推送完成: success={result['success']}, fail={result['fail']}")
        else:
            print("\n[QQ] 推送未启用，检查 data/qq_config.json")
    except ImportError:
        print("\n[QQ] 推送模块未安装")
    except Exception as e:
        print(f"\n[QQ] 推送异常: {e}")


if __name__ == "__main__":
    main()
