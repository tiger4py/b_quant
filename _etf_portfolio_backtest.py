"""Alpha #042 策略 — ETF 组合回测（与股票版完全相同的引擎）

用法:
    python _etf_portfolio_backtest.py                          # 默认参数
    python _etf_portfolio_backtest.py --max-positions 3        # 最多3只
    python _etf_portfolio_backtest.py --start 2023-01-01       # 指定起始日期
"""
import os, sys, csv, argparse
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from backtest import get_strategy
from backtest.portfolio import run_portfolio_backtest


def load_etf_bars(etf_dir="data/etf"):
    """读取所有ETF CSV，返回 (bars_by_code, stock_map)"""
    bars_by_code = defaultdict(list)
    base = os.path.join(ROOT_DIR, etf_dir)

    csv_files = []
    for root, dirs, files in os.walk(base):
        for f in files:
            if f.endswith(".csv"):
                csv_files.append(os.path.join(root, f))

    csv_files.sort()
    print(f"读取 {len(csv_files)} 个CSV文件...")

    for fp in csv_files:
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row["code"]
                bars_by_code[code].append({
                    "trade_date": row["trade_date"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "amount": float(row["amount"]),
                })

    # 按日期排序，过滤太短的
    stock_map = {}
    valid_bars = {}
    for code in sorted(bars_by_code):
        bars = sorted(bars_by_code[code], key=lambda b: b["trade_date"])
        if len(bars) >= 200:  # 至少200根K线
            valid_bars[code] = bars
            # 从etf_codes_main.json取名称
            stock_map[code] = {"code": code, "name": code, "market": code[:2]}

    # 补充名称
    main_path = os.path.join(ROOT_DIR, "data", "etf_codes_main.json")
    if os.path.exists(main_path):
        import json
        with open(main_path, "r", encoding="utf-8") as f:
            etf_list = json.load(f)
        for etf in etf_list:
            if etf["code"] in stock_map:
                stock_map[etf["code"]]["name"] = etf["name"]

    # 排除跨境ETF（港股/美股/日经/中概等）
    CROSS_BORDER_KW = ["港股", "恒生", "纳指", "标普", "日经", "中概", "H股",
                       "跨境", "德国", "法国", "越南", "印度"]
    removed = 0
    for code in list(valid_bars.keys()):
        name = stock_map.get(code, {}).get("name", "")
        if any(kw in name for kw in CROSS_BORDER_KW):
            del valid_bars[code]
            del stock_map[code]
            removed += 1
    if removed:
        print(f"已排除 {removed} 只跨境ETF")

    return valid_bars, stock_map


def main():
    parser = argparse.ArgumentParser(description="ETF 组合回测 — Alpha042")
    parser.add_argument("--strategy", default="alpha042")
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=15, help="显示TOP N")
    args = parser.parse_args()

    strategy = get_strategy(args.strategy)

    print("=" * 80)
    print(f"Alpha #042 策略 — ETF 组合回测")
    print(f"策略: {strategy.META['name']}  |  最大持仓: {args.max_positions}  |  初始资金: {args.initial_cash:,.0f}")
    print("=" * 80)

    # 加载数据
    bars_by_code, stock_map = load_etf_bars()

    # 日期过滤
    if args.start or args.end:
        for code in list(bars_by_code.keys()):
            bars = bars_by_code[code]
            if args.start:
                bars = [b for b in bars if b["trade_date"] >= args.start]
            if args.end:
                bars = [b for b in bars if b["trade_date"] <= args.end]
            if len(bars) >= 200:
                bars_by_code[code] = bars
            else:
                del bars_by_code[code]

    print(f"有效ETF: {len(bars_by_code)} 只  |  数据范围: 2022-05 ~ 2026-07\n")

    # 跑回测
    result = run_portfolio_backtest(
        bars_by_code=bars_by_code,
        stock_map=stock_map,
        strategy=strategy,
        initial_cash=args.initial_cash,
        max_positions=args.max_positions,
        enable_position_sizing=False,
        enable_circuit_breaker=False,
    )

    summary = result["summary"]
    selection = result["stock_summaries"]

    # ======== 输出 ========
    print(f"{'=' * 80}")
    print(f"回测结果")
    print(f"{'=' * 80}")
    print(f"  初始资金:     {summary['initial_cash']:>12,.0f}")
    print(f"  最终权益:     {summary['final_equity']:>12,.0f}")
    print(f"  总收益率:     {summary['total_return_pct']:>+11.2f}%")
    print(f"  最大回撤:     {summary['max_drawdown_pct']:>11.2f}%")
    print(f"  交易次数:     {summary['trade_count']:>12}")
    print(f"  胜率:         {summary['win_rate_pct']:>11.2f}%")
    print(f"  平均盈利:     {summary['avg_profit_pct']:>+11.2f}%")
    print(f"  盈亏比:       {summary['profit_factor']:>12}")

    # 按个股汇总
    print(f"\n{'=' * 80}")
    print(f"个股收益排名 (TOP {args.top})")
    print(f"{'=' * 80}")
    print(f"{'排名':<5} {'代码':<12} {'名称':<22} {'盈亏':>10} {'交易':>5} {'胜率':>8}")
    print("-" * 70)

    for rank, s in enumerate(selection[:args.top], 1):
        print(f"{rank:<5} {s['code']:<12} {s['name']:<22} {s['profit']:>+10,.0f} "
              f"{s['trade_count']:>4}笔 {s['win_rate_pct']:>7.1f}%")

    # 亏损TOP
    bottom = selection[-args.top:] if len(selection) > args.top else []
    if bottom:
        print(f"\n{'=' * 80}")
        print(f"个股亏损排名 (BOTTOM {min(args.top, len(bottom))})")
        print(f"{'=' * 80}")
        print(f"{'排名':<5} {'代码':<12} {'名称':<22} {'盈亏':>10} {'交易':>5} {'胜率':>8}")
        print("-" * 70)
        for rank, s in enumerate(reversed(bottom), 1):
            print(f"{rank:<5} {s['code']:<12} {s['name']:<22} {s['profit']:>+10,.0f} "
                  f"{s['trade_count']:>4}笔 {s['win_rate_pct']:>7.1f}%")

    # 分类汇总
    print(f"\n{'=' * 80}")
    print(f"分类表现")
    print(f"{'=' * 80}")
    categories = {
        "宽基": ["沪深300", "中证500", "中证1000", "中证2000", "上证50", "创业板", "科创50", "A500", "综指", "深证100"],
        "半导体": ["芯片", "半导体"],
        "医药": ["医药", "医疗", "创新药", "中药", "生物医药"],
        "证券保险": ["证券", "券商", "非银"],
        "消费": ["酒", "消费", "食品", "家电", "畜牧", "养殖", "农业"],
        "科技TMT": ["通信", "5G", "计算机", "传媒", "游戏", "人工智能", "云计算", "软件"],
        "能源材料": ["煤炭", "电力", "新能源", "光伏", "有色", "稀土", "钢铁", "电池"],
        "金融地产": ["银行", "地产", "金融"],
        "红利价值": ["红利", "低波", "价值"],
        "跨境": ["港股", "恒生", "纳指", "中概", "日经", "标普", "H股"],
        "商品": ["黄金"],
        "其他": ["军工", "汽车", "机械", "机器人", "旅游", "环保", "化工"],
    }

    for cat_name, keywords in categories.items():
        cat_stocks = [s for s in selection if any(kw in s["name"] for kw in keywords)]
        if not cat_stocks:
            continue
        total_profit = sum(s["profit"] for s in cat_stocks)
        total_trades = sum(s["trade_count"] for s in cat_stocks)
        wins = sum(s["wins"] for s in cat_stocks)
        wr = wins / total_trades * 100 if total_trades else 0
        print(f"  {cat_name:<10s}: {len(cat_stocks):>3}只  "
              f"总盈亏{total_profit:>+10,.0f}  "
              f"交易{total_trades:>4}笔  胜率{wr:>5.1f}%")

    # 全部列表（简洁）
    print(f"\n{'=' * 80}")
    print(f"全部 {len(selection)} 只ETF 收益一览")
    print(f"{'=' * 80}")
    for s in selection:
        bar = "█" * max(1, int(s["profit"] / 5000)) if s["profit"] > 0 else ""
        print(f"  {s['code']} {s['name']:<22s} {s['profit']:>+10,.0f}  {s['trade_count']:>3}笔  {bar}")


if __name__ == "__main__":
    main()
