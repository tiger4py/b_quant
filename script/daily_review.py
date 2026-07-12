#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个股买卖复盘 — 逐笔分析买卖逻辑 + 明日操作计划 + 回望反思对错。

用法:
  python script/daily_review.py              # 生成今日报告，打印到控制台
  python script/daily_review.py --push       # 生成并QQ推送
  python script/daily_review.py --date 2026-06-30  # 查看历史某天
  python script/daily_review.py --last 5     # 查看最近5天
  python script/daily_review.py --save-only  # 只保存文件，不打印
"""
import sys
import json
import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.portfolio import _build_market_stats
from logic.backtest_cache import load_latest_strategy_result
from script.daily_guide import assess_market, screen_candidates as screen_v_reversal

REVIEWS_DIR = ROOT_DIR / "data" / "reviews"


def _weekday_str(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{date_str} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def _load_stock_data(sess, latest_date, days=200):
    """加载全市场K线数据和股票列表。"""
    # 活跃股票
    latest_rows = (
        sess.query(StockInfo, StockDaily)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date == latest_date)
        .all()
    )
    stock_map = {
        stock.code: {"code": stock.code, "name": stock.name, "market": stock.market}
        for stock, _ in latest_rows
    }

    # 最近N天K线
    date_rows = (
        sess.query(StockDaily.trade_date)
        .distinct()
        .order_by(desc(StockDaily.trade_date))
        .limit(days)
        .all()
    )
    if not date_rows:
        return stock_map, {}, latest_date
    cutoff = min(row[0] for row in date_rows)

    rows = (
        sess.query(StockDaily)
        .join(StockInfo, StockDaily.code == StockInfo.code)
        .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date >= cutoff)
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )

    bars_by_code = defaultdict(list)
    for row in rows:
        bars_by_code[row.code].append({
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
        })

    return stock_map, bars_by_code, latest_date


def _load_portfolio():
    """读取实盘持仓。"""
    pf_path = ROOT_DIR / "data" / "portfolio.json"
    if not pf_path.exists():
        return {"cash": 0, "max_positions": 5, "positions": []}
    with open(pf_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_latest_price(sess, code):
    """获取某只股票的最新收盘价。"""
    row = (
        sess.query(StockDaily)
        .filter(StockDaily.code == code)
        .order_by(desc(StockDaily.trade_date))
        .first()
    )
    if row:
        return row.close, row.trade_date
    return None, None


def _load_trade_log():
    """读取交易日志。"""
    tl_path = ROOT_DIR / "data" / "trade_log.json"
    if not tl_path.exists():
        return []
    with open(tl_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _pair_trades(trade_log):
    """将买入和卖出配对，形成完整的交易记录。"""
    # 按股票分组
    by_stock = defaultdict(list)
    for t in trade_log:
        by_stock[t.get("code", "unknown")].append(t)

    closed = []
    for code, trades in by_stock.items():
        trades.sort(key=lambda x: (x.get("date", ""), 0 if x.get("action") == "buy" else 1))
        # 简单的 FIFO 配对
        buy_stack = []
        for t in trades:
            if t.get("action") == "buy":
                buy_stack.append(t)
            elif t.get("action") == "sell":
                if buy_stack:
                    buy = buy_stack.pop(0)
                    sell_price = t.get("price", 0)
                    buy_price = buy.get("price", 0)
                    pnl_pct = (sell_price / buy_price - 1) * 100 if buy_price > 0 else 0
                    try:
                        bd = datetime.strptime(buy.get("date", ""), "%Y-%m-%d")
                        sd = datetime.strptime(t.get("date", ""), "%Y-%m-%d")
                        hold_days = (sd - bd).days
                    except:
                        hold_days = "?"
                    closed.append({
                        "code": code,
                        "name": t.get("name", code),
                        "buy_date": buy.get("date", ""),
                        "buy_price": buy_price,
                        "sell_date": t.get("date", ""),
                        "sell_price": sell_price,
                        "pnl_pct": round(pnl_pct, 2),
                        "hold_days": hold_days,
                        "buy_reason": buy.get("reason", ""),
                        "sell_reason": t.get("reason", ""),
                    })
    closed.sort(key=lambda x: x.get("sell_date", ""), reverse=True)
    return closed


def _find_buy_for_sell(sell_trade, trade_log):
    """为卖出记录找到对应的买入记录。"""
    code = sell_trade.get("code", "")
    sell_date = sell_trade.get("date", "")
    # 找同日或之前最近的买入
    candidates = [
        t for t in trade_log
        if t.get("code") == code
        and t.get("action") == "buy"
        and t.get("date", "") <= sell_date
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.get("date", ""), reverse=True)
    return candidates[0]


def _load_past_reviews(days=5):
    """加载过去N天的复盘报告。"""
    past = []
    if REVIEWS_DIR.exists():
        files = sorted(REVIEWS_DIR.glob("*.md"), reverse=True)
        today_str = datetime.now().strftime("%Y-%m-%d")
        for f in files:
            if f.stem >= today_str:
                continue
            if len(past) >= days:
                break
            try:
                content = f.read_text(encoding="utf-8")
                past.append({"date": f.stem, "content": content})
            except:
                pass
    return list(reversed(past))


def _load_trading_journal():
    """读取交易日记。"""
    jp = ROOT_DIR / "data" / "trading_journal.json"
    if not jp.exists():
        return []
    with open(jp, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_report(target_date=None):
    """生成每日回顾报告，返回 (report_md, report_data_dict)。"""
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        # 确定目标日期
        if target_date:
            latest = target_date
        else:
            latest = sess.query(func.max(StockDaily.trade_date)).scalar()
        if not latest:
            return "# 每日回顾\n\n错误: 数据库无数据\n", {}

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ======== 1. 大盘评估 ========
        stock_map, bars_by_code, _ = _load_stock_data(sess, latest)
        market_stats = _build_market_stats(bars_by_code)
        market = assess_market(market_stats, latest)

        # ======== 2. market_bottom 信号 ========
        mb_result = load_latest_strategy_result("market_bottom")
        mb_trades = mb_result.get("trades", []) if mb_result else []
        mb_summary = mb_result.get("summary", {}) if mb_result else {}
        mb_buys_today = [t for t in mb_trades if t.get("buy_date") == latest]
        mb_sells_today = [t for t in mb_trades if t.get("sell_date") == latest and t.get("sell_reason") != "期末持仓"]
        mb_open = [t for t in mb_trades if t.get("sell_reason") == "期末持仓"]

        # ======== 3. V反候选（快速扫描） ========
        v_reversal_candidates = []
        if bars_by_code:
            v_reversal_candidates = screen_v_reversal(bars_by_code, stock_map, latest)
            # 简单评分排序（取前10）
            for c in v_reversal_candidates:
                c["simple_score"] = (
                    c["v_recovery_ratio"] * 30
                    + c["vol_ratio"] * 25
                    + c["vol_confirm_ratio"] * 20
                    + (1 - c["v_decline_pct"] / 20) * 15
                    + c["vol_trend_ratio"] * 10
                )
            v_reversal_candidates.sort(key=lambda x: x["simple_score"], reverse=True)

        # ======== 4. 实盘持仓 ========
        portfolio = _load_portfolio()
        holdings = []
        total_market_value = 0
        for pos in portfolio.get("positions", []):
            price, price_date = _get_latest_price(sess, pos["code"])
            if price and pos.get("buy_price", 0) > 0:
                pnl_pct = (price / pos["buy_price"] - 1) * 100
                market_value = price * pos["shares"]
                total_market_value += market_value
                holdings.append({
                    "code": pos["code"],
                    "name": pos.get("name", pos["code"]),
                    "shares": pos["shares"],
                    "buy_price": pos["buy_price"],
                    "buy_date": pos.get("buy_date", ""),
                    "current_price": price,
                    "price_date": price_date,
                    "pnl_pct": round(pnl_pct, 2),
                    "market_value": market_value,
                    "strategy_source": pos.get("strategy_source", ""),
                })
            elif pos.get("buy_price", 0) > 0:
                holdings.append({
                    "code": pos["code"],
                    "name": pos.get("name", pos["code"]),
                    "shares": pos["shares"],
                    "buy_price": pos["buy_price"],
                    "buy_date": pos.get("buy_date", ""),
                    "current_price": None,
                    "price_date": None,
                    "pnl_pct": None,
                    "market_value": None,
                    "strategy_source": pos.get("strategy_source", ""),
                })

        cash = portfolio.get("cash", 0)
        total_assets = cash + total_market_value

        # ======== 5. 今日操作（从 trade_log） ========
        trade_log = _load_trade_log()
        today_trades = [t for t in trade_log if t.get("date") == latest]
        today_buys = [t for t in today_trades if t.get("action") == "buy"]
        today_sells = [t for t in today_trades if t.get("action") == "sell"]

        # ======== 6. 最近操作（最近20天） ========
        all_dates = sorted(set(t.get("date", "") for t in trade_log), reverse=True)
        recent_dates = all_dates[:20]
        recent_trades = [t for t in trade_log if t.get("date") in recent_dates]

        # ======== 7. 近期完成的交易（有买有卖配对） ========
        closed_trades = _pair_trades(trade_log)

        # ======== 8. 获取每只股票的多周期K线用于复盘 ========
        def _get_stock_kline(sess, code, days=60):
            rows = (
                sess.query(StockDaily)
                .filter(StockDaily.code == code)
                .order_by(desc(StockDaily.trade_date))
                .limit(days)
                .all()
            )
            return list(reversed(rows))

        # ======== 构建报告 ========
        signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
        lines = []
        lines.append(f"# 个股买卖复盘 {_weekday_str(latest)}")
        lines.append(f"")
        lines.append(f"> 生成时间: {now_str}")
        lines.append(f"")

        # -- 大盘概况（精简） --
        lines.append(f"## 一、大盘概况")
        lines.append(f"")
        sig = market.get("signal", "RED")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 信号灯 | {signal_label.get(sig, sig)} (评分{market.get('composite_score', '-')}) |")
        lines.append(f"| 涨跌比 | {market.get('breadth_current', '-')} |")
        lines.append(f"| 跌停数 | {market.get('limit_down_current', '-')} |")
        lines.append(f"| 成交额 | {market.get('amount_current', 0)/1e8:.0f}亿 |")
        lines.append(f"| 评估 | {market.get('summary', '-')} |")
        lines.append(f"")

        # ======== 核心：今日买入个股复盘 ========
        lines.append(f"## 二、今日买入 — 个股复盘")
        lines.append(f"")

        if today_buys:
            for t in today_buys:
                code = t.get("code", "")
                name = t.get("name", code)
                buy_price = t.get("price", 0)
                shares = t.get("shares", 0)
                reason = t.get("reason", "（未记录）")
                amount = t.get("amount", buy_price * shares)

                # 获取该股K线做简单复盘
                klines = _get_stock_kline(sess, code, 60)
                cur_price_t, _ = _get_latest_price(sess, code)

                lines.append(f"### 🔴 买入: {name} ({code})")
                lines.append(f"")
                lines.append(f"| 项目 | 内容 |")
                lines.append(f"|------|------|")
                lines.append(f"| 买入价 | {buy_price:.2f} 元 |")
                lines.append(f"| 数量 | {shares} 股 |")
                lines.append(f"| 金额 | {amount:,.0f} 元 |")
                lines.append(f"| 买入理由 | {reason} |")
                if cur_price_t:
                    pnl_since = (cur_price_t / buy_price - 1) * 100
                    sign = "+" if pnl_since >= 0 else ""
                    lines.append(f"| 现价 | {cur_price_t:.2f} 元 ({sign}{pnl_since:.1f}%) |")
                lines.append(f"")

                # 技术面快速复盘
                if klines and len(klines) >= 10:
                    closes = [r.close for r in klines]
                    highs = [r.high for r in klines]
                    lows = [r.low for r in klines]
                    vols = [r.volume or 0 for r in klines]

                    ma20 = sum(closes[-20:]) / min(20, len(closes[-20:])) if len(closes) >= 20 else sum(closes)/len(closes)
                    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
                    high_20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
                    low_20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
                    vol_5 = sum(vols[-5:]) / 5
                    vol_20 = sum(vols[-20:]) / min(20, len(vols[-20:]))
                    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

                    pos_in_range = (buy_price - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50

                    lines.append(f"**买入位置分析:**")
                    lines.append(f"")
                    lines.append(f"| 指标 | 数值 | 评估 |")
                    lines.append(f"|------|------|------|")
                    lines.append(f"| 20日位置 | {pos_in_range:.0f}% | {'高位追入⚠️' if pos_in_range > 70 else ('低位买入✅' if pos_in_range < 30 else '中位')} |")
                    lines.append(f"| MA5 | {ma5:.2f} | {'站上MA5 ✅' if buy_price > ma5 else '跌破MA5 ⚠️'} |")
                    lines.append(f"| MA20 | {ma20:.2f} | {'站上MA20 ✅' if buy_price > ma20 else '跌破MA20 ⚠️'} |")
                    lines.append(f"| 量比(5/20) | {vol_ratio:.1f}x | {'放量 🔥' if vol_ratio > 1.5 else ('缩量' if vol_ratio < 0.7 else '正常')} |")
                    lines.append(f"| 20日最高 | {high_20:.2f} | 距高点 {(high_20/buy_price-1)*100:+.1f}% |")
                    lines.append(f"| 20日最低 | {low_20:.2f} | 距低点 {(buy_price/low_20-1)*100:+.1f}% |")
                    lines.append(f"")

                    lines.append(f"**⏭ 持股观察要点:**")
                    lines.append(f"- 止损位: {low_20:.2f} (20日低点) 或 {buy_price*0.93:.2f} (-7%)")
                    lines.append(f"- 目标位: {high_20:.2f} (20日高点, +{(high_20/buy_price-1)*100:.1f}%)")
                    lines.append(f"- 关键均线: MA20={ma20:.2f}, {'守住' if buy_price > ma20 else '需收复'}")
                lines.append(f"")
        else:
            lines.append(f"今日无买入操作")
            lines.append(f"")

        # -- 策略买入信号参考 --
        if mb_buys_today:
            lines.append(f"### 📋 策略买入信号参考 (market_bottom)")
            lines.append(f"")
            for t in mb_buys_today[:5]:
                lines.append(f"- {t['name']}({t['code']}) {t.get('buy_price', '-')}元 | {t.get('buy_reason', '')}")
            lines.append(f"")

        # ======== 今日卖出个股复盘 ========
        lines.append(f"## 三、今日卖出 — 个股复盘")
        lines.append(f"")

        if today_sells:
            for t in today_sells:
                code = t.get("code", "")
                name = t.get("name", code)
                sell_price = t.get("price", 0)
                shares = t.get("shares", 0)
                reason = t.get("reason", "（未记录）")

                # 查找对应的买入记录
                buy_info = _find_buy_for_sell(t, trade_log)
                buy_price = buy_info.get("price", 0) if buy_info else 0
                buy_date = buy_info.get("date", "?") if buy_info else "?"

                if buy_price > 0:
                    pnl_pct = (sell_price / buy_price - 1) * 100
                    hold_days = "?"
                    try:
                        bd = datetime.strptime(buy_date, "%Y-%m-%d")
                        sd = datetime.strptime(t.get("date", latest), "%Y-%m-%d")
                        hold_days = (sd - bd).days
                    except:
                        pass
                else:
                    pnl_pct = 0
                    hold_days = "?"

                pnl_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪")

                lines.append(f"### {pnl_emoji} 卖出: {name} ({code})")
                lines.append(f"")
                lines.append(f"| 项目 | 内容 |")
                lines.append(f"|------|------|")
                lines.append(f"| 卖出价 | {sell_price:.2f} 元 |")
                lines.append(f"| 数量 | {shares} 股 |")
                lines.append(f"| 买入价 | {buy_price:.2f} 元 ({buy_date}) |" if buy_price > 0 else "| 买入价 | 未找到买入记录 |")
                sign = "+" if pnl_pct >= 0 else ""
                lines.append(f"| **盈亏** | **{sign}{pnl_pct:.1f}%** |")
                lines.append(f"| 持仓天数 | {hold_days} 天 |")
                lines.append(f"| 卖出理由 | {reason} |")
                lines.append(f"")

                # 复盘卖出质量
                if buy_price > 0:
                    lines.append(f"**卖出复盘:**")
                    lines.append(f"")

                    # 卖出后走势（如有）
                    klines = _get_stock_kline(sess, code, 30)
                    if klines:
                        sell_date = t.get("date", "")
                        after_closes = [r.close for r in klines if r.trade_date > sell_date]
                        if after_closes:
                            # 卖出后N天走势
                            after_3d = after_closes[min(2, len(after_closes)-1)]
                            after_5d = after_closes[min(4, len(after_closes)-1)]
                            ret_3d = (after_3d / sell_price - 1) * 100
                            ret_5d = (after_5d / sell_price - 1) * 100
                            sell_quality_3d = "✅ 卖对了" if ret_3d < 0 else "⚠️ 卖飞了"
                            sell_quality_5d = "✅ 卖对了" if ret_5d < 0 else "⚠️ 卖飞了"
                            lines.append(f"| 卖出后3日 | {ret_3d:+.1f}% | {sell_quality_3d} |")
                            lines.append(f"| 卖出后5日 | {ret_5d:+.1f}% | {sell_quality_5d} |")
                        else:
                            lines.append(f"| 卖出后走势 | 暂无数据（今日卖出） |")
                    lines.append(f"")

                    # 总结
                    if pnl_pct > 5:
                        lines.append(f"📝 **总结**: 盈利 {sign}{pnl_pct:.1f}%，这笔交易不错。反思：当初买入逻辑是否成立？是否可以加仓？")
                    elif pnl_pct > 0:
                        lines.append(f"📝 **总结**: 小盈 {sign}{pnl_pct:.1f}%。反思：是否过早止盈？是否达到预期目标？")
                    elif pnl_pct > -5:
                        lines.append(f"📝 **总结**: 小亏 {pnl_pct:.1f}%。反思：买入时机是否合适？止损是否及时？")
                    else:
                        lines.append(f"📝 **总结**: 亏损 {pnl_pct:.1f}%。⚠️ 需要重点复盘：买入逻辑哪里出了问题？为什么没早止损？")
                lines.append(f"")
        else:
            lines.append(f"今日无卖出操作")
            lines.append(f"")

        # -- 策略卖出信号参考 --
        if mb_sells_today:
            lines.append(f"### 📋 策略卖出信号参考 (market_bottom)")
            lines.append(f"")
            for t in mb_sells_today:
                sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                lines.append(f"- {t['name']}({t['code']}) 盈亏{sign}{t.get('profit_pct', 0):.1f}% → {t.get('sell_reason', '')}")
            lines.append(f"")

        # ======== 持仓个股逐一复盘 ========
        lines.append(f"## 四、持仓个股 — 逐一复盘")
        lines.append(f"")
        lines.append(f"现金: {cash:,.0f} | 持仓市值: {total_market_value:,.0f} | 总资产: {total_assets:,.0f} | 仓位: {total_market_value/max(1,total_assets)*100:.0f}%")
        lines.append(f"")

        if holdings:
            for h in holdings:
                name = h["name"]
                code = h["code"]
                cost = h["buy_price"]
                cur_p = h["current_price"]
                pnl = h["pnl_pct"]
                buy_date = h.get("buy_date", "")
                src = h.get("strategy_source", "")

                if cur_p and pnl is not None:
                    sign = "+" if pnl >= 0 else ""
                    pnl_emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")

                    lines.append(f"### {pnl_emoji} {name} ({code})")
                    lines.append(f"")
                    lines.append(f"| 项目 | 内容 |")
                    lines.append(f"|------|------|")
                    lines.append(f"| 成本 | {cost:.2f} 元 |")
                    lines.append(f"| 现价 | {cur_p:.2f} 元 |")
                    lines.append(f"| 盈亏 | **{sign}{pnl:.1f}%** |")
                    lines.append(f"| 买入日 | {buy_date} |")
                    lines.append(f"| 来源 | {src or '-'} |")

                    # 持仓天数
                    if buy_date:
                        try:
                            bd = datetime.strptime(buy_date, "%Y-%m-%d")
                            ld = datetime.strptime(latest, "%Y-%m-%d")
                            hold_days = (ld - bd).days
                            lines.append(f"| 持仓天 | {hold_days} 天 |")
                        except:
                            pass
                    lines.append(f"")

                    # K线复盘
                    klines = _get_stock_kline(sess, code, 60)
                    if klines and len(klines) >= 10:
                        closes = [r.close for r in klines]
                        highs = [r.high for r in klines]
                        lows = [r.low for r in klines]
                        vols = [r.volume or 0 for r in klines]

                        n = len(closes)
                        ret_5d = (closes[-1]/closes[-5]-1)*100 if n >= 5 else 0
                        ret_10d = (closes[-1]/closes[-10]-1)*100 if n >= 10 else 0
                        high_20 = max(highs[-20:]) if n >= 20 else max(highs)
                        low_20 = min(lows[-20:]) if n >= 20 else min(lows)
                        ma20 = sum(closes[-20:])/20 if n >= 20 else sum(closes)/n

                        # 趋势判断
                        xs = list(range(n))
                        mx = sum(xs)/n; my = sum(closes)/n
                        ss_xy = sum((x-mx)*(y-my) for x,y in zip(xs, closes))
                        ss_xx = sum((x-mx)**2 for x in xs)
                        slope = (ss_xy/ss_xx)/closes[0]*100 if ss_xx>0 and closes[0]>0 else 0

                        trend_label = "↗ 上升" if slope > 0.1 else ("↘ 下降" if slope < -0.1 else "→ 横盘")

                        lines.append(f"**近期走势:**")
                        lines.append(f"")
                        lines.append(f"| 指标 | 数值 |")
                        lines.append(f"|------|------|")
                        lines.append(f"| 5日收益 | {ret_5d:+.1f}% |")
                        lines.append(f"| 10日收益 | {ret_10d:+.1f}% |")
                        lines.append(f"| 趋势 | {trend_label} ({slope:+.2f}%/d) |")
                        lines.append(f"| 20日高 | {high_20:.2f} |")
                        lines.append(f"| 20日低 | {low_20:.2f} |")
                        lines.append(f"| vs MA20 | {'✅ 线上' if cur_p > ma20 else '⚠️ 线下'} ({ma20:.2f}) |")

                    lines.append(f"")
                    lines.append(f"**⏭ 操作建议:**")
                    if pnl > 10:
                        lines.append(f"- 浮盈较大({sign}{pnl:.1f}%)，考虑移动止盈，保护利润")
                        lines.append(f"- 止盈参考: {cur_p*0.93:.2f} (回撤-7%止盈)")
                    elif pnl > 0:
                        lines.append(f"- 小幅盈利中，观察是否能继续持有")
                        lines.append(f"- 止损上移到成本价 {cost:.2f}，确保不亏")
                    elif pnl > -5:
                        lines.append(f"- 小幅亏损，还在正常范围")
                        lines.append(f"- 止损位: {cost*0.93:.2f} (-7%)，跌破果断走")
                    else:
                        lines.append(f"- ⚠️ 亏损较大({sign}{pnl:.1f}%)，需要认真评估")
                        lines.append(f"- 问自己: 买入逻辑还在吗？如果不在，立刻止损")
                    lines.append(f"")
        else:
            lines.append(f"(空仓 — 等待下一個機會)")
            lines.append(f"")

        # ======== 近期完成交易复盘 ========
        lines.append(f"## 五、近期完成交易 — 整体复盘")
        lines.append(f"")

        if closed_trades:
            # 最近30天完成的交易
            recent_closed = [t for t in closed_trades if t.get("sell_date", "") >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")]
            if recent_closed:
                total_pnl = sum(t.get("pnl_pct", 0) for t in recent_closed)
                wins = [t for t in recent_closed if t.get("pnl_pct", 0) > 0]
                losses = [t for t in recent_closed if t.get("pnl_pct", 0) <= 0]
                avg_win = sum(t["pnl_pct"] for t in wins)/len(wins) if wins else 0
                avg_loss = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0

                lines.append(f"| 统计 | 数值 |")
                lines.append(f"|------|------|")
                lines.append(f"| 近30天完成交易 | {len(recent_closed)} 笔 |")
                lines.append(f"| 盈利笔数 | {len(wins)} 笔 |")
                lines.append(f"| 亏损笔数 | {len(losses)} 笔 |")
                lines.append(f"| 胜率 | {len(wins)/max(1,len(recent_closed))*100:.0f}% |")
                lines.append(f"| 平均盈利 | {avg_win:+.1f}% |")
                lines.append(f"| 平均亏损 | {avg_loss:+.1f}% |")
                lines.append(f"| 累计盈亏 | {total_pnl:+.1f}% |")
                lines.append(f"")

                lines.append(f"**逐笔明细:**")
                lines.append(f"")
                lines.append(f"| 股票 | 买入日 | 卖出日 | 持仓天 | 盈亏 | 评判 |")
                lines.append(f"|------|--------|--------|--------|------|------|")
                for t in recent_closed[:15]:
                    pnl = t.get("pnl_pct", 0)
                    sign = "+" if pnl >= 0 else ""
                    judgement = "✅" if pnl > 5 else ("👍" if pnl > 0 else ("⚠️" if pnl > -5 else "❌"))
                    hold = t.get("hold_days", "?")
                    lines.append(f"| {t.get('name','?')} | {t.get('buy_date','?')} | {t.get('sell_date','?')} | {hold} | {sign}{pnl:.1f}% | {judgement} |")
                lines.append(f"")
        else:
            lines.append(f"暂无完成的交易记录")
            lines.append(f"")

        # ======== V反候选关注 ========
        if v_reversal_candidates:
            strong = [c for c in v_reversal_candidates if c["simple_score"] >= 45]
            if strong:
                lines.append(f"## 六、V反候选 — 明日观察名单")
                lines.append(f"")
                lines.append(f"| 股票 | 形态 | 评分 | 量比 |")
                lines.append(f"|------|------|------|------|")
                for c in strong[:8]:
                    lines.append(f"| {c['name']} | {c.get('v_label','')} | {c['simple_score']:.0f} | {c.get('vol_ratio',0):.1f}x |")
                lines.append(f"")

        # ======== 七、明日操作计划 ========
        lines.append(f"## 七、明日操作计划")
        lines.append(f"")

        # 根据信号灯给出操作框架
        if sig == "GREEN":
            lines.append(f"**操作基调:** 🟢 积极 | 可新开仓 | 仓位上限: 80%")
            max_new = portfolio.get("max_positions", 5) - len(holdings)
            if max_new > 0:
                lines.append(f"可新增仓位: {max_new} 个")
        elif sig == "YELLOW":
            lines.append(f"**操作基调:** 🟡 谨慎 | 精选开仓 | 仓位上限: 50%")
            lines.append(f"优先管理现有持仓，新开仓需满足更严格条件")
        else:
            lines.append(f"**操作基调:** 🔴 防守 | 不新开仓 | 仓位上限: 30%")
            lines.append(f"专注持仓风控，不追逐任何买入信号")
        lines.append(f"")

        # 具体操作清单
        lines.append(f"### 📋 操作清单")
        lines.append(f"")

        # 1. 持仓管理动作
        if holdings:
            lines.append(f"**一、持仓管理:**")
            lines.append(f"")
            for h in holdings:
                pnl = h.get("pnl_pct", 0) or 0
                code = h["code"]
                name = h["name"]
                cost = h["buy_price"]
                cur_p = h.get("current_price", cost)

                klines = _get_stock_kline(sess, code, 30)
                if klines and len(klines) >= 5:
                    closes = [r.close for r in klines]
                    highs = [r.high for r in klines]
                    lows = [r.low for r in klines]
                    low_10 = min(lows[-10:]) if len(lows) >= 10 else min(lows)
                    high_10 = max(highs[-10:]) if len(highs) >= 10 else max(highs)
                    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes)/len(closes)
                else:
                    low_10 = high_10 = ma10 = cur_p

                lines.append(f"**{name}** ({code}) | 成本{cost:.2f} 现价{cur_p:.2f} | 盈亏{pnl:+.1f}%")
                lines.append(f"")
                lines.append(f"| 动作 | 触发条件 | 价位 |")
                lines.append(f"|------|----------|------|")
                lines.append(f"| 🛑 止损 | 跌破10日低点 或 -7% | **{min(low_10, cost*0.93):.2f}** |")
                lines.append(f"| ⏸ 减仓 | 跌破MA10 | {ma10:.2f} |")
                lines.append(f"| 🎯 止盈 | 浮盈>15%后回撤-7% | {cur_p*0.93:.2f} |" if pnl > 10 else f"| 🎯 目标 | 10日高点 | {high_10:.2f} |")
                lines.append(f"| ✅ 持有 | 线上运行 | — |")
                lines.append(f"")
        else:
            lines.append(f"**一、空仓等待:** 无持仓需要管理")
            lines.append(f"")

        # 2. 明日关注买入候选
        lines.append(f"**二、买入候选观察:**")
        lines.append(f"")
        candidates_tomorrow = []

        # 从 V反强信号收集
        if v_reversal_candidates:
            for c in v_reversal_candidates:
                if c.get("simple_score", 0) >= 45:
                    candidates_tomorrow.append({
                        "name": c["name"], "code": c["code"],
                        "source": f"V反({c.get('v_label','')})",
                        "score": c.get("simple_score", 0),
                        "trigger": f"开盘量比>1.2 或 盘中放量突破",
                    })

        # 从 market_bottom 买入信号收集
        if mb_buys_today:
            for t in mb_buys_today[:5]:
                # 避免重复
                if not any(c["code"] == t["code"] for c in candidates_tomorrow):
                    candidates_tomorrow.append({
                        "name": t["name"], "code": t["code"],
                        "source": "market_bottom",
                        "score": 50,
                        "trigger": t.get("buy_reason", "信号触发"),
                    })

        if candidates_tomorrow:
            lines.append(f"| 优先级 | 股票 | 来源 | 买入触发条件 |")
            lines.append(f"|--------|------|------|-------------|")
            for i, c in enumerate(candidates_tomorrow[:8], 1):
                priority = "🔥" if i <= 2 else ("⭐" if i <= 4 else "👀")
                lines.append(f"| {priority} | {c['name']}({c['code']}) | {c['source']} | {c['trigger']} |")
        else:
            lines.append(f"(暂无候选 — 等待信号出现)")
        lines.append(f"")

        # 3. 交易纪律提醒
        lines.append(f"**三、纪律提醒:**")
        lines.append(f"")
        lines.append(f"- [ ] 单只股票仓位不超过总资产 20%")
        lines.append(f"- [ ] 买入前确认: 理由写下来了吗？止损位设好了吗？")
        lines.append(f"- [ ] 盘中不做情绪化交易")
        lines.append(f"- [ ] 收盘前检查所有持仓是否触发止损/止盈条件")
        lines.append(f"")

        # ======== 八、回望复盘 ========
        lines.append(f"## 八、回望复盘 — 对在哪 / 错在哪")
        lines.append(f"")

        # 8.1 回顾前几天的操作建议是否应验
        past_reviews = _load_past_reviews(5)
        if past_reviews:
            lines.append(f"### 📅 近期复盘回顾")
            lines.append(f"")
            for pr in past_reviews:
                lines.append(f"**{pr['date']}** ({_weekday_str(pr['date'])}) 的复盘回顾:")
                lines.append(f"")

                # 提取当天提到的关键股票/操作，检查后续走势
                content = pr["content"]
                # 简单查找提到的股票代码
                stock_refs = set(re.findall(r'\((sh\.\d{6}|sz\.\d{6})\)', content))
                if stock_refs:
                    for code in list(stock_refs)[:5]:
                        cur_p, _ = _get_latest_price(sess, code)
                        # 找该日期附近的价格
                        hist_row = (
                            sess.query(StockDaily)
                            .filter(StockDaily.code == code, StockDaily.trade_date <= pr["date"])
                            .order_by(desc(StockDaily.trade_date))
                            .first()
                        )
                        if hist_row and cur_p:
                            ret_since = (cur_p / hist_row.close - 1) * 100
                            name = hist_row.close  # placeholder
                            # get name
                            info = sess.query(StockInfo).filter(StockInfo.code == code).first()
                            name = info.name if info else code
                            arrow = "✅" if ret_since > 0 else "❌"
                            lines.append(f"- {arrow} {name}({code}): 复盘日{hist_row.close:.2f} → 现在{cur_p:.2f} ({ret_since:+.1f}%)")
                    lines.append(f"")
                else:
                    lines.append(f"  (无明确股票引用)")
                    lines.append(f"")

        # 8.2 近期操作的自我审视
        lines.append(f"### 🔍 近期操作自我审视")
        lines.append(f"")

        recent_closed_30d = [t for t in closed_trades if t.get("sell_date", "") >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")] if closed_trades else []

        if recent_closed_30d:
            wins = [t for t in recent_closed_30d if t.get("pnl_pct", 0) > 0]
            losses = [t for t in recent_closed_30d if t.get("pnl_pct", 0) <= 0]

            lines.append(f"**近30天完成 {len(recent_closed_30d)} 笔交易:**")
            lines.append(f"")

            # 分析盈利的单子 — 做对了什么
            if wins:
                lines.append(f"#### ✅ 做对了什么（盈利交易分析）")
                lines.append(f"")
                for w in wins[:5]:
                    lines.append(f"- **{w['name']}**: {w['buy_date']}买 → {w['sell_date']}卖, **+{w['pnl_pct']:.1f}%**, 持{w.get('hold_days','?')}天")
                    if w.get("buy_reason"):
                        lines.append(f"  - 买入逻辑: {w['buy_reason']}")
                    if w.get("sell_reason"):
                        lines.append(f"  - 卖出逻辑: {w['sell_reason']}")
                lines.append(f"")
                lines.append(f"**共性总结:** 盈利交易普遍具备什么特征？（回看当时的买入理由、大盘环境、持股时长）")
                lines.append(f"")

            # 分析亏损的单子 — 做错了什么
            if losses:
                lines.append(f"#### ❌ 做错了什么（亏损交易反思）")
                lines.append(f"")
                for l in losses[:5]:
                    lines.append(f"- **{l['name']}**: {l['buy_date']}买 → {l['sell_date']}卖, **{l['pnl_pct']:.1f}%**, 持{l.get('hold_days','?')}天")
                    if l.get("buy_reason"):
                        lines.append(f"  - 买入逻辑: {l['buy_reason']}")
                    if l.get("sell_reason"):
                        lines.append(f"  - 卖出逻辑: {l['sell_reason']}")

                    # 检查卖出后走势（是否卖错了）
                    klines = _get_stock_kline(sess, l['code'], 60)
                    if klines and l.get("sell_date"):
                        after = [r.close for r in klines if r.trade_date > l["sell_date"]]
                        if after:
                            ret_after = (after[-1] / l["sell_price"] - 1) * 100
                            if ret_after > 3:
                                lines.append(f"  - ⚠️ 卖出后涨了{ret_after:+.1f}% — 卖早了/止损太紧")
                            elif ret_after < -5:
                                lines.append(f"  - ✅ 卖出后继续跌{ret_after:+.1f}% — 卖得对")
                lines.append(f"")
                lines.append(f"**亏损原因归纳:** 是买入时机问题？止损太慢？还是策略信号失效？")
                lines.append(f"")
        else:
            lines.append(f"近30天无完成交易，暂无足够样本回顾")
            lines.append(f"")

        # 8.3 交易日记中的反思
        journal = _load_trading_journal()
        if journal:
            recent_journal = [j for j in journal if j.get("date", "") >= (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")]
            if recent_journal:
                lines.append(f"### 📝 近期交易日记摘要")
                lines.append(f"")
                for j in recent_journal[:5]:
                    lines.append(f"- **{j.get('date', '')}** | 情绪: {j.get('emotion', '-')} | 操作: {j.get('action', '-')}")
                    if j.get("lesson"):
                        lines.append(f"  > 教训: {j['lesson']}")
                    if j.get("plan"):
                        lines.append(f"  > 计划: {j['plan']}")
                lines.append(f"")

        # 8.4 策略回测 vs 实盘对比
        if mb_result and mb_summary:
            lines.append(f"### 📊 策略回测表现")
            lines.append(f"")
            lines.append(f"| 策略 | 累计收益 | 最大回撤 | 胜率 | 交易数 |")
            lines.append(f"|------|----------|----------|------|--------|")
            lines.append(f"| market_bottom | {mb_summary.get('total_return_pct','-')}% | {mb_summary.get('max_drawdown_pct','-')}% | {mb_summary.get('win_rate_pct','-')}% | {mb_summary.get('trade_count',0)} |")
            lines.append(f"")
            lines.append(f"**对比反思:** 实盘是否跑赢了策略回测？如果没有，差距在哪？（执行偏差/滑点/心态）")
            lines.append(f"")

        lines.append(f"---")
        lines.append(f"*免责声明: 基于历史数据的量化分析，不构成投资建议。投资有风险，入市需谨慎。*")

        report = "\n".join(lines)

        # 构建结构化数据（供外部使用）
        data = {
            "date": latest,
            "generated_at": now_str,
            "market": market,
            "market_bottom": {
                "buys_today": mb_buys_today,
                "sells_today": mb_sells_today,
                "open_positions": mb_open,
                "summary": mb_summary,
            },
            "v_reversal": {
                "candidates": v_reversal_candidates[:20],
                "strong_count": len([c for c in v_reversal_candidates if c["simple_score"] >= 45]),
                "watch_count": len([c for c in v_reversal_candidates if 35 <= c["simple_score"] < 45]),
            },
            "portfolio": {
                "cash": cash,
                "total_assets": total_assets,
                "holdings": holdings,
            },
            "trades_today": {"buys": today_buys, "sells": today_sells},
        }

        return report, data

    finally:
        sess.close()


def save_report(report, date_str):
    """保存报告到 data/reviews/YYYY-MM-DD.md"""
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"{date_str}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[review] 报告已保存: {path}")
    return path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="个股买卖复盘")
    parser.add_argument("--date", type=str, default=None, help="指定日期 (YYYY-MM-DD)")
    parser.add_argument("--last", type=int, default=0, help="查看最近N天")
    parser.add_argument("--push", action="store_true", help="推送到QQ")
    parser.add_argument("--save-only", action="store_true", help="只保存文件，不打印")
    args = parser.parse_args()

    if args.last > 0:
        # 列出最近N天
        if REVIEWS_DIR.exists():
            files = sorted(REVIEWS_DIR.glob("*.md"), reverse=True)[:args.last]
            for f in files:
                print(f"  {f.stem}")
        else:
            print("(无历史报告)")
        return

    if args.date:
        # 查看历史某天
        path = REVIEWS_DIR / f"{args.date}.md"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                print(f.read())
        else:
            print(f"[!] 报告不存在: {args.date}")
        return

    # 生成今日报告
    print("=" * 60)
    print("  个股买卖复盘 — 生成中...")
    print("=" * 60)

    report, data = generate_report()

    if not args.save_only:
        print("\n" + report)

    # 保存
    save_report(report, data["date"])

    # QQ推送
    if args.push:
        print("\n[推送] 发送到QQ...")
        try:
            from models.qq_webhook import QQPusher
            pusher = QQPusher()
            if pusher.enabled:
                # 推送精简版（取报告前2000字符）
                short_report = report[:1800]
                if len(report) > 1800:
                    short_report += "\n\n... (完整报告见 data/reviews/)"
                r = pusher.push_long_text(short_report)
                print(f"  QQ推送完成: success={r['success']}, fail={r['fail']}")
            else:
                print("  QQ推送未启用")
        except Exception as e:
            print(f"  QQ推送失败: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
