# -*- coding: utf-8 -*-
"""
ETF 全市场回测脚本 — 从 CSV 加载 ETF 日线数据，运行策略回测，结果存文件

数据源: data/etf/YYYY/YYYY-MM.csv（按月存储，每行一只 ETF 一天的数据）
策略:   backtest/strategy/strategy_etf_*.py（自动发现）
输出:   data/strategy/{strategy_id}/{年}-{月}/{日期}_{序号}.json

用法:
  # 默认回测（全量 ETF，2022-05-06 起）
  python script/run_etf_backtest.py --strategy etf_alpha

  # 指定参数
  python script/run_etf_backtest.py --strategy etf_vegas --start 2023-01-01 --max-positions 3

  # 列出可用 ETF 策略
  python script/run_etf_backtest.py --list
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest import get_strategy, run_portfolio_backtest
from backtest.registry import list_strategies

# ============ 默认参数 ============

DEFAULT_START_DATE = "2022-05-06"
DEFAULT_INITIAL_CASH = 1000000.0
DEFAULT_MAX_POSITIONS = 5
MIN_BAR_COUNT = 120       # 最少需要 120 条日线才纳入回测
ETF_DATA_DIR = ROOT_DIR / "data" / "etf"
ARCHIVE_ROOT = ROOT_DIR / "data" / "strategy"

# 排除的非 A 股 ETF（名称包含以下关键词则跳过）
ETF_NAME_BLACKLIST = [
    "纳指", "港股", "恒生", "中概", "标普", "道琼",
    "德国", "日经", "法国", "印度", "越南", "韩国",
]


# ============ 数据加载 ============

def load_etf_bars(data_dir=None, start_date=None, end_date=None):
    """从 CSV 加载 ETF 日线数据。

    参数:
        data_dir: ETF CSV 目录，默认 data/etf/
        start_date: 起始日期 YYYY-MM-DD（含）
        end_date: 结束日期 YYYY-MM-DD（含）

    返回:
        (stocks, bars_by_code): stocks 为 [{code, name, market, ...}]，
                                bars_by_code 为 {code: [bar_dict, ...]}
    """
    data_dir = Path(data_dir or ETF_DATA_DIR)
    if not data_dir.exists():
        raise FileNotFoundError(f"ETF 数据目录不存在: {data_dir}")

    # 收集所有 ETF 数据
    etf_bars = {}
    etf_info = {}

    for year_dir in sorted(data_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_file in sorted(year_dir.glob("*.csv")):
            try:
                with open(month_file, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        code = row.get("code", "").strip()
                        trade_date = row.get("trade_date", "").strip()
                        if not code or not trade_date:
                            continue

                        # 日期过滤
                        if start_date and trade_date < start_date:
                            continue
                        if end_date and trade_date > end_date:
                            continue

                        try:
                            bar = {
                                "trade_date": trade_date,
                                "open": float(row.get("open") or 0),
                                "high": float(row.get("high") or 0),
                                "low": float(row.get("low") or 0),
                                "close": float(row.get("close") or 0),
                                "volume": int(float(row.get("volume") or 0)),
                                "amount": float(row.get("amount") or 0),
                            }
                        except (ValueError, TypeError):
                            continue

                        if bar["close"] <= 0:
                            continue

                        # 名称黑名单过滤（排除海外/商品 ETF）
                        etf_name = row.get("name", "")
                        if code not in etf_info:
                            if any(kw in etf_name for kw in ETF_NAME_BLACKLIST):
                                etf_info[code] = None  # 标记为排除
                            else:
                                etf_info[code] = {
                                    "code": code,
                                    "name": etf_name,
                                    "market": code[:2],
                                }
                        if etf_info.get(code) is None:
                            continue

                        etf_bars.setdefault(code, []).append(bar)
            except Exception as e:
                print(f"[WARN] 读取 {month_file} 失败: {e}")
                continue

    # 排序 + 过滤数据不足的 ETF
    stocks = []
    clean_bars = {}
    for code, bars in etf_bars.items():
        if len(bars) < MIN_BAR_COUNT:
            continue
        bars.sort(key=lambda b: b["trade_date"])
        info = etf_info[code]
        info["daily_count"] = len(bars)
        info["latest_trade_date"] = bars[-1]["trade_date"]
        stocks.append(info)
        clean_bars[code] = bars

    stocks.sort(key=lambda x: x["code"])
    print(f"[ETF数据] {len(stocks)} 只 ETF，共 {sum(len(b) for b in clean_bars.values())} 条日线")
    if stocks:
        all_dates = set()
        for bars in clean_bars.values():
            for b in bars:
                all_dates.add(b["trade_date"])
        print(f"[ETF数据] 日期范围: {min(all_dates)} ~ {max(all_dates)}")
    return stocks, clean_bars


# ============ 结果归档 ============

def _save_archive(strategy_id, result):
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
    print(f"[归档] {strategy_id} -> {archive_path}")
    return archive_path


# ============ 主流程 ============

def run_etf_backtest(strategy_id, start_date=None, end_date=None,
                     initial_cash=DEFAULT_INITIAL_CASH, max_positions=DEFAULT_MAX_POSITIONS):
    """运行 ETF 回测并返回结果。"""
    strategy = get_strategy(strategy_id)
    print(f"[策略] {strategy.META['id']}: {strategy.META['name']}")

    # 加载 ETF 数据
    stocks, bars_by_code = load_etf_bars(start_date=start_date, end_date=end_date)
    if not stocks:
        raise ValueError("没有找到可用于 ETF 回测的数据")

    stock_map = {s["code"]: s for s in stocks}

    # 运行组合回测
    result = run_portfolio_backtest(
        bars_by_code, stock_map, strategy,
        initial_cash=initial_cash, max_positions=max_positions,
    )

    # 整理交易记录
    trades = result["trades"]
    trades.sort(key=lambda x: (x["buy_date"], x["code"]))

    # 当前持仓快照（从 trades 中提取期末持仓）
    current_positions = []
    for t in trades:
        if t.get("sell_reason") == "期末持仓":
            current_positions.append({
                "code": t["code"],
                "name": t["name"],
                "buy_date": t["buy_date"],
                "buy_price": t["buy_price"],
                "cur_price": t["sell_price"],
                "shares": t["shares"],
                "cost": t["buy_amount"],
                "market_value": t["sell_amount"],
                "profit": t["profit"],
                "profit_pct": t["profit_pct"],
            })
    current_positions.sort(key=lambda x: x["profit"], reverse=True)

    # 确定实际日期范围
    all_dates = set()
    for bars in bars_by_code.values():
        for b in bars:
            all_dates.add(b["trade_date"])
    date_list = sorted(all_dates)
    latest_date = date_list[-1] if date_list else end_date or "unknown"

    output = {
        "strategy": strategy.META,
        "selection": {
            "stock_count": len(stocks),
            "start_date": start_date or DEFAULT_START_DATE,
            "end_date": end_date or latest_date,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
            "latest_trade_date": latest_date,
            "criteria": f"全部 ETF，剔除 K 线不足 {MIN_BAR_COUNT} 条的品种",
            "min_bar_count": MIN_BAR_COUNT,
            "data_source": str(ETF_DATA_DIR),
        },
        "summary": result["summary"],
        "equity_curve": result["equity_curve"],
        "stock_summaries": result["stock_summaries"],
        "trades": trades,
        "current_positions": current_positions,
        "market_gate": result.get("market_gate"),
    }

    # 归档
    _save_archive(strategy_id, output)
    return output


# ============ 打印结果 ============

def _print_result(result, top_n=10):
    """打印单个策略的回测结果。"""
    selection = result["selection"]
    summary = result["summary"]
    print(f"  策略: {result['strategy']['id']} — {result['strategy']['name']}")
    print(
        f"  ETF 数: {selection['stock_count']} | "
        f"区间: {selection['start_date']} ~ {selection['end_date']} | "
        f"最大持仓: {selection['max_positions']}"
    )
    print(
        f"  收益: {summary['total_return_pct']:+.2f}% | "
        f"回撤: {summary['max_drawdown_pct']:.2f}% | "
        f"胜率: {summary['win_rate_pct']:.2f}% | "
        f"交易: {summary['trade_count']} 笔 | "
        f"均盈: {summary['avg_profit_pct']:+.2f}% | "
        f"PF: {summary['profit_factor']}"
    )

    stock_summaries = result.get("stock_summaries", [])
    if stock_summaries:
        print(f"  Top {top_n} ETF:")
        for item in stock_summaries[:top_n]:
            print(
                f"    {item['code']:12s} {item['name']:16s} "
                f"盈亏={item['profit']:+,.0f}  "
                f"交易={item['trade_count']}笔  "
                f"胜率={item['win_rate_pct']:.0f}%  "
                f"均持={item['avg_hold_days']:.0f}天"
            )

    gate = result.get("market_gate")
    if gate and gate.get("blocked_days", 0) > 0:
        print(f"  [市场择时] 允许={gate['allowed_days']}天 "
              f"阻止={gate['blocked_days']}天 "
              f"({gate['allowed_rate_pct']:.1f}% 允许)")

    # 当前持仓快照
    holdings = result.get("current_positions", [])
    if holdings:
        total_cost = sum(h["cost"] for h in holdings)
        total_value = sum(h["market_value"] for h in holdings)
        total_pnl = sum(h["profit"] for h in holdings)
        print(f"\n  >>> 当前持仓快照 ({len(holdings)}只) <<<")
        print(f"  {'代码':12s} {'名称':14s} {'买入日':10s} {'成本':>8s} {'现价':>8s} {'数量':>6s} {'市值':>10s} {'盈亏':>8s} {'盈亏%':>7s}")
        print(f"  {'-'*85}")
        for h in holdings:
            print(
                f"  {h['code']:12s} {h['name']:14s} {h['buy_date']:10s} "
                f"{h['buy_price']:>8.3f} {h['cur_price']:>8.3f} {h['shares']:>6d} "
                f"{h['market_value']:>10,.0f} {h['profit']:>+8,.0f} {h['profit_pct']:>+6.1f}%"
            )
        print(f"  {'-'*85}")
        print(
            f"  {'合计':12s} {'':14s} {'':10s} {'':8s} {'':8s} {'':6s} "
            f"{total_value:>10,.0f} {total_pnl:>+8,.0f}"
        )
        # 验证: cash + holdings_value = equity
        cash = result["equity_curve"][-1]["cash"] if result.get("equity_curve") else 0
        equity = result["equity_curve"][-1]["equity"] if result.get("equity_curve") else 0
        print(f"  现金: {cash:,.0f}  +  持仓市值: {total_value:,.0f}  =  总权益: {cash + total_value:,.0f}  (记录: {equity:,.0f})")


def _print_comparison(results):
    """打印多策略对比汇总表。"""
    if len(results) < 2:
        return
    print(f"\n{'='*90}")
    print(f"{'策略':22s} {'收益':>8s} {'回撤':>8s} {'胜率':>8s} {'交易':>6s} {'均盈':>8s} {'PF':>6s} {'终值':>12s}")
    print(f"{'-'*90}")
    for r in results:
        s = r["summary"]
        print(
            f"{r['strategy']['id']:22s} "
            f"{s['total_return_pct']:>+7.2f}% "
            f"{s['max_drawdown_pct']:>7.2f}% "
            f"{s['win_rate_pct']:>7.2f}% "
            f"{s['trade_count']:>6d} "
            f"{s['avg_profit_pct']:>+7.2f}% "
            f"{s['profit_factor']:>6.2f} "
            f"{s['final_equity']:>12,.0f}"
        )
    print(f"{'='*90}")


# ============ CLI ============

def main():
    parser = argparse.ArgumentParser(
        description="ETF 全市场策略回测（数据源：data/etf/ CSV 文件）。"
                    "不指定 --strategy 时，默认跑全部 ETF 策略。"
    )
    parser.add_argument("--strategy", default=None,
                        help="策略 ID（如 etf_alpha, etf_vegas）。不指定则跑全部 ETF 策略。")
    parser.add_argument("--list", action="store_true", help="列出所有可用的 ETF 相关策略")
    parser.add_argument("--start", default=DEFAULT_START_DATE,
                        help=f"起始日期 YYYY-MM-DD（默认 {DEFAULT_START_DATE}）")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认：数据最新日期）")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH,
                        help=f"初始资金（默认 {DEFAULT_INITIAL_CASH:,.0f}）")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help=f"最大持仓数（默认 {DEFAULT_MAX_POSITIONS}）")
    parser.add_argument("--top", type=int, default=10, help="打印前 N 只 ETF 的统计（默认 10）")
    args = parser.parse_args()

    # --list: 列出可用策略
    if args.list:
        all_strategies = list_strategies()
        etf_strategies = [s for s in all_strategies if s["id"].startswith("etf_")]
        if etf_strategies:
            print("可用的 ETF 策略:")
            for s in etf_strategies:
                print(f"  {s['id']:20s} — {s['name']}")
                print(f"  {'':20s}   {s['description'][:80]}...")
        else:
            print("未找到 ETF 策略（id 以 'etf_' 开头）")
            print("所有已注册策略:")
            for s in all_strategies:
                print(f"  {s['id']}")
        return

    # 确定要跑的策略列表
    if args.strategy:
        strategy_ids = [args.strategy]
    else:
        # 不指定策略 → 跑全部 ETF 策略
        all_strategies = list_strategies()
        strategy_ids = [s["id"] for s in all_strategies if s["id"].startswith("etf_")]
        if not strategy_ids:
            print("未找到 ETF 策略（id 以 'etf_' 开头），用 --list 查看可用策略")
            return
        strategy_ids.sort()

    # 统一加载 ETF 数据（所有策略共享）
    print(f"即将运行 {len(strategy_ids)} 个策略: {', '.join(strategy_ids)}")
    stocks, bars_by_code = load_etf_bars(start_date=args.start, end_date=args.end)
    if not stocks:
        print("没有找到可用于 ETF 回测的数据")
        return
    stock_map = {s["code"]: s for s in stocks}

    # 逐个策略运行
    results = []
    for sid in strategy_ids:
        strategy = get_strategy(sid)
        print(f"\n{'—'*60}")
        print(f"[{sid}] {strategy.META['name']}")

        result = run_portfolio_backtest(
            bars_by_code, stock_map, strategy,
            initial_cash=args.initial_cash, max_positions=args.max_positions,
        )

        # 整理交易记录
        trades = result["trades"]
        trades.sort(key=lambda x: (x["buy_date"], x["code"]))

        # 当前持仓快照（从 trades 中提取期末持仓）
        current_positions = []
        for t in trades:
            if t.get("sell_reason") == "期末持仓":
                current_positions.append({
                    "code": t["code"],
                    "name": t["name"],
                    "buy_date": t["buy_date"],
                    "buy_price": t["buy_price"],
                    "cur_price": t["sell_price"],
                    "shares": t["shares"],
                    "cost": t["buy_amount"],
                    "market_value": t["sell_amount"],
                    "profit": t["profit"],
                    "profit_pct": t["profit_pct"],
                })
        # 按盈亏降序
        current_positions.sort(key=lambda x: x["profit"], reverse=True)

        # 确定实际日期范围
        all_dates = set()
        for bars in bars_by_code.values():
            for b in bars:
                all_dates.add(b["trade_date"])
        date_list = sorted(all_dates)
        latest_date = date_list[-1] if date_list else args.end or "unknown"

        output = {
            "strategy": strategy.META,
            "selection": {
                "stock_count": len(stocks),
                "start_date": args.start or DEFAULT_START_DATE,
                "end_date": args.end or latest_date,
                "initial_cash": args.initial_cash,
                "max_positions": args.max_positions,
                "max_position_cash": round(args.initial_cash / args.max_positions, 2),
                "latest_trade_date": latest_date,
                "criteria": f"全部 ETF，剔除 K 线不足 {MIN_BAR_COUNT} 条的品种",
                "min_bar_count": MIN_BAR_COUNT,
                "data_source": str(ETF_DATA_DIR),
            },
            "summary": result["summary"],
            "equity_curve": result["equity_curve"],
            "stock_summaries": result["stock_summaries"],
            "trades": trades,
            "current_positions": current_positions,
            "market_gate": result.get("market_gate"),
        }

        _save_archive(sid, output)
        _print_result(output, top_n=args.top)
        results.append(output)

    # 汇总对比
    if len(results) >= 2:
        _print_comparison(results)

    # 全部策略总交易汇总
    if results:
        total_trades = sum(len(r["trades"]) for r in results)
        print(f"\n全部 {len(results)} 个策略共产生 {total_trades} 笔交易。")


if __name__ == "__main__":
    main()
