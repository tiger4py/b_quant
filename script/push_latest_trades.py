#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取趋势跟随策略回测的最新交易信号并推送QQ。

以数据库最新日期为准，如果当天没有交易信号则说明"无需操作"，
同时始终展示当前持仓。
"""
import sys
import os
from pathlib import Path
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily
from logic.backtest_cache import load_market_backtest_cache

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

STRATEGY_ID = "market_bottom"


def _weekday(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{date_str} ({['周一','周二','周三','周四','周五','周六','周日'][dt.weekday()]})"
    except Exception:
        return date_str


def main():
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        result = load_market_backtest_cache(sess, STRATEGY_ID)
        if not result:
            print(f"[!] 回测缓存不存在，请先运行回测")
            return

        trades = result.get("trades", [])
        summary = result.get("summary", {})
        selection = result.get("selection", {})

        if not trades:
            print("[!] 回测结果无交易记录")
            return

        # 数据库最新日期
        db_latest = sess.query(func.max(StockDaily.trade_date)).scalar()
        today = db_latest or selection.get("latest_trade_date", "")

        # 当天买卖
        buys_today = [t for t in trades if t.get("buy_date") == today]
        sells_today = [t for t in trades if t.get("sell_date") == today and t.get("sell_reason") != "期末持仓"]
        has_action = bool(buys_today or sells_today)

        # 当前持仓
        open_positions = [t for t in trades if t.get("sell_reason") == "期末持仓"]

        # ---- 控制台输出 ----
        print(f"{'=' * 60}")
        print(f"  趋势跟随策略 — {_weekday(today)}")
        print(f"{'=' * 60}")
        print(f"  收益: {summary.get('total_return_pct', '-')}% | 回撤: {summary.get('max_drawdown_pct', '-')}% | 胜率: {summary.get('win_rate_pct', '-')}%")
        print()

        if has_action:
            if sells_today:
                print(f"  ┌─ 卖出 ({len(sells_today)} 只) ──")
                for t in sells_today:
                    sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                    print(f"  │ [卖] {t['code']} {t['name']}  盈亏 {sign}{t.get('profit_pct', 0):.1f}% → {t.get('sell_reason', '')}")
                print(f"  └─")

            if buys_today:
                if sells_today:
                    print()
                print(f"  ┌─ 买入 ({len(buys_today)} 只) ──")
                for t in buys_today:
                    print(f"  │ [买] {t['code']} {t['name']}  {t['buy_price']:.2f}元 × {t['shares']}股 = {t['buy_amount']:,.0f}")
                    print(f"  │     {t.get('buy_reason', '')}")
                print(f"  └─")
        else:
            print(f"  今日无买卖信号，无需操作")
            print()

        print(f"  ┌─ 当前持仓 ({len(open_positions)} 只) ──")
        if open_positions:
            for t in open_positions:
                sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                print(f"  │ {t['code']} {t['name']}  买入 {t['buy_date']} 价{t['buy_price']:.2f} | {sign}{t.get('profit_pct', 0):.1f}%")
        else:
            print(f"  │ (空仓)")
        print(f"  └─")

        # ---- QQ推送 ----
        lines = [
            f"[*] 趋势跟随 — {_weekday(today)}",
            f"收益 {summary.get('total_return_pct', '-')}% | 回撤 {summary.get('max_drawdown_pct', '-')}% | 胜率 {summary.get('win_rate_pct', '-')}%",
            "",
        ]

        if has_action:
            if sells_today:
                lines.append(f"--- 卖出 ({len(sells_today)}只) ---")
                for t in sells_today:
                    sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                    lines.append(f"[卖] {t['name']}({t['code']}) {sign}{t.get('profit_pct', 0):.1f}% → {t.get('sell_reason', '')}")

            if buys_today:
                lines.append("")
                lines.append(f"--- 买入 ({len(buys_today)}只) ---")
                for t in buys_today:
                    lines.append(f"[买] {t['name']}({t['code']}) {t['buy_price']:.2f}元×{t['shares']}股")
                    reason = t.get('buy_reason', '')
                    if len(reason) > 80:
                        reason = reason[:77] + "..."
                    lines.append(f"    {reason}")
        else:
            lines.append("今日无买卖信号，无需操作")

        if open_positions:
            lines.append("")
            lines.append(f"--- 当前持仓 ({len(open_positions)}只) ---")
            for t in open_positions:
                sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                lines.append(f"  {t['name']}({t['code']}) {sign}{t.get('profit_pct', 0):.1f}%")

        lines.append("")
        lines.append("--- 趋势跟随回测 · 仅供参考 ---")
        msg = "\n".join(lines)

        print(f"\n[推送内容预览]")
        print(msg)

        # 推送到QQ
        try:
            from models.qq_webhook import QQPusher
            pusher = QQPusher()
            if pusher.enabled:
                r = pusher.push_long_text(msg)
                print(f"\n[QQ] 推送完成: success={r['success']}, fail={r['fail']}")
            else:
                print("\n[QQ] 推送未启用")
        except Exception as e:
            print(f"\n[QQ] 推送异常: {e}")

    finally:
        sess.close()


if __name__ == "__main__":
    main()
