#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易复盘工具 — 分析历史交易，评估策略执行质量。

用法:
  python script/review_trades.py                  # 总览（全部交易汇总）
  python script/review_trades.py --month 2026-07  # 按月复盘
  python script/review_trades.py --stats           # 按策略来源统计
  python script/review_trades.py --export review.xlsx  # 导出Excel
"""
import sys
import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

TRADE_LOG = ROOT_DIR / "data" / "trade_log.json"
PORTFOLIO_FILE = ROOT_DIR / "data" / "portfolio.json"


def _load_trades():
    if not TRADE_LOG.exists():
        return []
    with open(TRADE_LOG, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_portfolio():
    if not PORTFOLIO_FILE.exists():
        return {"cash": 0, "max_positions": 5, "positions": []}
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_pct(val):
    if val is None:
        return "-"
    return f"{val:+.1f}%"


def overview(trades):
    """打印交易总览。"""
    buys = [t for t in trades if t.get("action") == "buy"]
    sells = [t for t in trades if t.get("action") == "sell"]

    print(f"{'=' * 60}")
    print(f"  交易总览")
    print(f"{'=' * 60}")
    print(f"  总买入: {len(buys)}笔 | 总卖出: {len(sells)}笔")
    print(f"")

    # 按策略来源统计
    by_source = defaultdict(lambda: {"buy": 0, "sell": 0, "pnl": 0.0, "pnls": []})
    for t in buys:
        src = t.get("strategy_source", "") or "manual"
        by_source[src]["buy"] += 1
    for t in sells:
        pnl_pct = t.get("pnlPct", 0)
        src = "manual"  # sell records don't have strategy_source; match by buy
        # Try to find matching buy
        code = t.get("code")
        for b in buys:
            if b.get("code") == code and b.get("date") <= t.get("date", ""):
                src = b.get("strategy_source", "") or "manual"
                break
        by_source[src]["sell"] += 1
        by_source[src]["pnl"] += t.get("pnl", 0) or 0
        by_source[src]["pnls"].append(pnl_pct)

    print(f"  {'策略来源':<18} {'买入':>5} {'卖出':>5} {'胜率':>7} {'累计盈亏':>12} {'平均盈亏':>8}")
    print(f"  {'-' * 58}")
    for src in sorted(by_source):
        s = by_source[src]
        wins = sum(1 for p in s["pnls"] if p > 0)
        wr = f"{wins / len(s['pnls']) * 100:.0f}%" if s["pnls"] else "-"
        avg = sum(s["pnls"]) / len(s["pnls"]) if s["pnls"] else 0
        print(f"  {src:<18} {s['buy']:>5} {s['sell']:>5} {wr:>7} {s['pnl']:>12,.0f} {avg:>+7.1f}%")

    # 大盘信号分布
    market_buys = defaultdict(int)
    for t in buys:
        ms = t.get("market_signal", "") or "未知"
        market_buys[ms] += 1
    if any(k != "未知" for k in market_buys):
        print(f"")
        print(f"  买入时大盘信号分布:")
        for sig in ["GREEN", "YELLOW", "RED", "未知"]:
            if market_buys.get(sig):
                print(f"    {sig}: {market_buys[sig]}笔")

    # 当前持仓
    pf = _load_portfolio()
    positions = pf.get("positions", [])
    if positions:
        print(f"")
        print(f"  当前持仓 ({len(positions)}只):")
        for p in positions:
            print(f"    {p.get('name', p['code'])} {p.get('shares', 0)}股 @ {p.get('buy_price', 0):.2f} ({p.get('buy_date', '')})")


def review_month(trades, year_month):
    """复盘指定月份的交易。"""
    buys = [t for t in trades if t.get("action") == "buy" and t.get("date", "").startswith(year_month)]
    sells = [t for t in trades if t.get("action") == "sell" and t.get("date", "").startswith(year_month)]

    print(f"{'=' * 60}")
    print(f"  月度复盘: {year_month}")
    print(f"{'=' * 60}")
    print(f"  买入: {len(buys)}笔 | 卖出: {len(sells)}笔")
    print(f"")

    if not buys and not sells:
        print(f"  本月无交易记录")
        return

    # 卖出盈亏
    if sells:
        total_pnl = sum(t.get("pnl", 0) or 0 for t in sells)
        wins = sum(1 for t in sells if (t.get("pnlPct", 0) or 0) > 0)
        print(f"  卖出汇总:")
        print(f"    总盈亏: {total_pnl:+,.0f}元 | 胜率: {wins}/{len(sells)} ({wins/len(sells)*100:.0f}%)")
        if sells:
            avg_pnl = sum(t.get("pnlPct", 0) or 0 for t in sells) / len(sells)
            print(f"    平均盈亏: {avg_pnl:+.1f}%")
        print(f"")

        print(f"  卖出明细:")
        print(f"  {'日期':<12} {'股票':<16} {'买入价':>8} {'卖出价':>8} {'盈亏%':>8} {'盈亏额':>10} {'原因'}")
        print(f"  {'-' * 75}")
        for t in sorted(sells, key=lambda x: x.get("date", "")):
            print(f"  {t.get('date', ''):<12} {t.get('name', t.get('code', '')):<16} "
                  f"{t.get('buyPrice', 0):>8.2f} {t.get('price', 0):>8.2f} "
                  f"{_fmt_pct(t.get('pnlPct')):>8} {t.get('pnl', 0):>10,.0f} "
                  f"{t.get('reason', '-')[:20]}")

    if buys:
        print(f"")
        print(f"  买入明细:")
        print(f"  {'日期':<12} {'股票':<16} {'价格':>8} {'股数':>6} {'来源':<16} {'大盘':>8} {'备注'}")
        print(f"  {'-' * 80}")
        for t in sorted(buys, key=lambda x: x.get("date", "")):
            src = t.get("strategy_source", "") or "手动"
            ms = t.get("market_signal", "") or "-"
            note = (t.get("decision_note", "") or t.get("reason", ""))[:25]
            print(f"  {t.get('date', ''):<12} {t.get('name', t.get('code', '')):<16} "
                  f"{t.get('price', 0):>8.2f} {t.get('shares', 0):>6} "
                  f"{src:<16} {ms:>8} {note}")

    # 策略合规度
    buys_with_src = [t for t in buys if t.get("strategy_source") and t.get("strategy_source") != "manual"]
    if buys_with_src:
        print(f"")
        print(f"  策略来源分布:")
        src_count = defaultdict(int)
        for t in buys_with_src:
            src_count[t["strategy_source"]] += 1
        for src, count in sorted(src_count.items(), key=lambda x: -x[1]):
            print(f"    {src}: {count}笔")


def export_to_csv(trades, output_path):
    """导出交易明细CSV。"""
    import csv
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["日期", "方向", "代码", "名称", "价格", "股数", "金额", "盈亏", "盈亏%",
                     "策略来源", "信号排名", "大盘信号", "原因", "备注"])
        for t in sorted(trades, key=lambda x: x.get("date", "")):
            action = "买入" if t.get("action") == "buy" else "卖出"
            w.writerow([
                t.get("date", ""),
                action,
                t.get("code", ""),
                t.get("name", ""),
                t.get("price", ""),
                t.get("shares", ""),
                t.get("amount", ""),
                t.get("pnl", "") if action == "卖出" else "",
                f"{t.get('pnlPct', '')}%" if action == "卖出" else "",
                t.get("strategy_source", ""),
                t.get("signal_rank", ""),
                t.get("market_signal", ""),
                t.get("reason", ""),
                t.get("decision_note", ""),
            ])
    print(f"导出: {output_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="交易复盘工具")
    parser.add_argument("--month", type=str, default=None, help="指定月份 (YYYY-MM)")
    parser.add_argument("--stats", action="store_true", help="按策略来源统计")
    parser.add_argument("--export", type=str, default=None, help="导出CSV路径")
    args = parser.parse_args()

    trades = _load_trades()
    if not trades:
        print("(无交易记录)")
        return

    if args.month:
        review_month(trades, args.month)
    elif args.stats:
        overview(trades)
    elif args.export:
        export_to_csv(trades, args.export)
    else:
        # 默认：总览 + 最近一月
        overview(trades)

        # 最近一月
        if trades:
            latest_month = max(t.get("date", "2000-01")[:7] for t in trades)
            print(f"\n")
            review_month(trades, latest_month)


if __name__ == "__main__":
    main()
