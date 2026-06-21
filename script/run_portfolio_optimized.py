#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合层优化回测 — 对比三种模式：

  1. 原始：单策略(趋势跟随)，固定 5 仓位
  2. 优化：单策略 + 仓位管理(MA60) + 熔断(-25%)
  3. 多策略：趋势跟随 + 波动率V反 轮动

用法:
  python script/run_portfolio_optimized.py              # 500天
  python script/run_portfolio_optimized.py --days 1000  # 1000天
"""
import sys
import argparse
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockInfo, StockDaily
from backtest.portfolio import run_portfolio_backtest, run_multi_strategy_backtest
from backtest import get_strategy
from logic.backtest_cache import load_market_bars

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def format_result(label, result, days):
    s = result["summary"]
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  收益: {s['total_return_pct']:+.2f}%  |  年化: {s['total_return_pct']/days*252:+.1f}%")
    print(f"  最大回撤: {s['max_drawdown_pct']:.2f}%  |  胜率: {s['win_rate_pct']:.2f}%")
    print(f"  交易数: {s['trade_count']}  |  平均盈利: {s['avg_profit_pct']:.2f}%")
    print(f"  利润因子: {s['profit_factor']}  |  终值: {s['final_equity']:,.0f}")

    # 分策略统计（多策略模式）
    if "strategy_breakdown" in result:
        print(f"\n  --- 分策略统计 ---")
        for sb in result["strategy_breakdown"]:
            print(f"  {sb['strategy_name']}: {sb['trade_count']}笔 | "
                  f"胜率{sb['win_rate_pct']:.1f}% | 盈利{sb['profit']:+,.0f}")

    # 权益曲线摘要
    eq = result["equity_curve"]
    if eq:
        # 每 50 天采样
        sampled = eq[::max(1, len(eq) // 20)]
        print(f"\n  --- 权益曲线采样 ---")
        for pt in sampled:
            ret = (pt["equity"] / eq[0]["equity"] - 1) * 100
            bar = "█" * max(1, int((ret + 50) / 5)) if ret > -50 else ""
            print(f"  {pt['date']}  {pt['equity']:>12,.0f}  {ret:+.1f}%  {bar}")


def main():
    parser = argparse.ArgumentParser(description="组合层优化回测对比")
    parser.add_argument("--days", type=int, default=500, help="回测天数")
    parser.add_argument("--cash", type=float, default=1000000.0, help="初始资金")
    parser.add_argument("--max-positions", type=int, default=5, help="最大持仓数")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        print(f"加载数据 (最近 {args.days} 天)...")
        stocks, bars_by_code, latest_date = load_market_bars(sess, args.days)
        stock_map = {s["code"]: s for s in stocks}
        print(f"股票: {len(stocks)} 只 | 最新日期: {latest_date}")

        tf_strategy = get_strategy("trend_following")
        vb_strategy = get_strategy("volatility_breakout")

        # ================================================================
        # 模式 1：原始 — 趋势跟随单策略，固定仓位
        # ================================================================
        print(f"\n{'#'*60}")
        print(f"# 模式 1: 趋势跟随（原始 — 固定仓位）")
        print(f"{'#'*60}")
        r1 = run_portfolio_backtest(
            bars_by_code, stock_map, tf_strategy,
            initial_cash=args.cash,
            max_positions=args.max_positions,
            enable_position_sizing=False,
            enable_circuit_breaker=False,
        )
        format_result("模式1: 趋势跟随(原始)", r1, args.days)

        # ================================================================
        # 模式 2：优化 — 趋势跟随 + 仓位管理 + 熔断
        # ================================================================
        print(f"\n{'#'*60}")
        print(f"# 模式 2: 趋势跟随 + 仓位管理(MA60) + 熔断(-25%)")
        print(f"{'#'*60}")
        r2 = run_portfolio_backtest(
            bars_by_code, stock_map, tf_strategy,
            initial_cash=args.cash,
            max_positions=args.max_positions,
            enable_position_sizing=True,
            enable_circuit_breaker=True,
        )
        format_result("模式2: 趋势跟随+仓位+熔断", r2, args.days)

        # ================================================================
        # 模式 3：多策略轮动 — 趋势跟随 + 波动率V反
        # ================================================================
        print(f"\n{'#'*60}")
        print(f"# 模式 3: 多策略轮动（趋势跟随 + 波动率V反）")
        print(f"{'#'*60}")
        r3 = run_multi_strategy_backtest(
            bars_by_code, stock_map,
            strategies=[tf_strategy, vb_strategy],
            initial_cash=args.cash,
            max_positions=args.max_positions,
            enable_position_sizing=True,
            enable_circuit_breaker=True,
        )
        format_result("模式3: 多策略轮动", r3, args.days)

        # ================================================================
        # 汇总对比
        # ================================================================
        print(f"\n{'='*60}")
        print(f"  汇总对比 ({args.days}天 | {args.max_positions}仓位)")
        print(f"{'='*60}")
        print(f"  {'模式':<30} {'收益':>8} {'回撤':>8} {'胜率':>8} {'PF':>6} {'交易':>6}")
        print(f"  {'-'*66}")
        for label, r in [
            ("1.趋势跟随(原始)", r1),
            ("2.趋势跟随+仓位+熔断", r2),
            ("3.多策略轮动", r3),
        ]:
            s = r["summary"]
            print(f"  {label:<30} {s['total_return_pct']:>+7.1f}% {s['max_drawdown_pct']:>7.1f}% "
                  f"{s['win_rate_pct']:>7.1f}% {s['profit_factor']:>5.2f} {s['trade_count']:>5}")

    finally:
        sess.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
