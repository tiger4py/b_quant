"""Alpha #042 策略 — ETF全量回测

读取 data/etf/ 下所有月度CSV，按ETF分组后逐只回测，输出收益排名。
"""
import os, sys, csv
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from backtest.strategy.strategy_alpha042 import generate_signals, _compute_metrics

# ======== 加载所有ETF数据 ========

def load_all_etf_data(etf_dir="data/etf"):
    """读取所有年月CSV，返回 {code: [bar_dicts]}"""
    etf_bars = defaultdict(list)
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
                etf_bars[code].append({
                    "trade_date": row["trade_date"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "amount": float(row["amount"]),
                    "name": row.get("name", ""),
                })

    # 按日期排序
    for code in etf_bars:
        etf_bars[code].sort(key=lambda b: b["trade_date"])

    return etf_bars


# ======== 单只ETF回测 ========

def backtest_one_etf(code, bars, init_cash=1_000_000):
    """回测单只ETF，返回收益统计"""
    signals = generate_signals(bars)
    if not signals:
        return None

    cash = init_cash
    shares = 0
    entry_price = 0
    trades = []
    wins = 0

    for sig in signals:
        bar = next((b for b in bars if b["trade_date"] == sig["date"]), None)
        if bar is None:
            continue
        close = bar["close"]

        if sig["action"] == "buy":
            shares = int(cash // close // 100 * 100)
            if shares > 0:
                cash -= shares * close
                entry_price = close
                trades.append({"entry_date": sig["date"], "entry_price": close,
                               "entry_reason": sig["reason"], "shares": shares})

        elif sig["action"] == "sell" and shares > 0:
            pnl = (close - entry_price) * shares
            cash += shares * close
            if pnl > 0:
                wins += 1
            if trades:
                trades[-1]["exit_date"] = sig["date"]
                trades[-1]["exit_price"] = close
                trades[-1]["pnl"] = pnl
                trades[-1]["pnl_pct"] = (close / entry_price - 1) * 100
                trades[-1]["exit_reason"] = sig["reason"]
            shares = 0

    # 如果最后还持仓，按最后一天收盘价平仓
    if shares > 0:
        final_close = bars[-1]["close"]
        pnl = (final_close - entry_price) * shares
        cash += shares * final_close
        if pnl > 0:
            wins += 1
        if trades:
            trades[-1]["exit_date"] = bars[-1]["trade_date"]
            trades[-1]["exit_price"] = final_close
            trades[-1]["pnl"] = pnl
            trades[-1]["pnl_pct"] = (final_close / entry_price - 1) * 100
            trades[-1]["exit_reason"] = "回测结束强制平仓"

    final_value = cash
    total_return = (final_value / init_cash - 1) * 100
    win_rate = wins / len(trades) * 100 if trades else 0

    return {
        "code": code,
        "name": bars[0].get("name", ""),
        "total_return": total_return,
        "trades": len(trades),
        "wins": wins,
        "win_rate": win_rate,
        "bars_count": len(bars),
        "date_range": f"{bars[0]['trade_date']} ~ {bars[-1]['trade_date']}",
        "trade_details": trades,
    }


# ======== 主流程 ========

def main():
    print("=" * 80)
    print("Alpha #042 策略 — ETF 全量回测")
    print("=" * 80)

    etf_bars = load_all_etf_data()
    total = len(etf_bars)
    print(f"共 {total} 只ETF\n")

    results = []
    min_bars = 200  # 至少200根K线

    for idx, (code, bars) in enumerate(sorted(etf_bars.items())):
        if len(bars) < min_bars:
            continue

        result = backtest_one_etf(code, bars)
        if result and result["trades"] >= 2:  # 至少2笔交易
            results.append(result)

        if (idx + 1) % 30 == 0:
            print(f"  进度: {idx+1}/{total}")

    # 按收益降序排列
    results.sort(key=lambda r: -r["total_return"])

    # ======== 输出 ========
    print(f"\n{'=' * 80}")
    print(f"回测结果 — 有效ETF: {len(results)} 只 (交易>=2笔)")
    print(f"{'=' * 80}")
    print(f"{'排名':<5} {'代码':<12} {'名称':<20} {'收益':>8} {'交易':>5} {'胜率':>7} {'数据范围'}")
    print("-" * 80)

    for rank, r in enumerate(results, 1):
        print(f"{rank:<5} {r['code']:<12} {r['name']:<20} {r['total_return']:>+7.1f}% "
              f"{r['trades']:>4}笔 {r['win_rate']:>6.1f}% {r['date_range']}")

    # 统计
    positive = sum(1 for r in results if r["total_return"] > 0)
    avg_ret = sum(r["total_return"] for r in results) / len(results) if results else 0
    avg_trades = sum(r["trades"] for r in results) / len(results) if results else 0
    avg_wr = sum(r["win_rate"] for r in results) / len(results) if results else 0

    print(f"\n{'=' * 80}")
    print(f"统计汇总:")
    print(f"  正收益: {positive}/{len(results)} ({positive/max(1,len(results))*100:.0f}%)")
    print(f"  平均收益: {avg_ret:+.1f}%")
    print(f"  平均交易: {avg_trades:.1f}笔")
    print(f"  平均胜率: {avg_wr:.1f}%")

    # 分组统计
    print(f"\n  收益分档:")
    for low, high, label in [(50, 999, ">50%"), (30, 50, "30~50%"), (10, 30, "10~30%"),
                               (0, 10, "0~10%"), (-999, 0, "<0%")]:
        cnt = sum(1 for r in results if low <= r["total_return"] < high)
        print(f"    {label}: {cnt} 只")

    # TOP/BOTTOM 10
    print(f"\n  TOP 10:")
    for r in results[:10]:
        print(f"    {r['code']} {r['name']:<20s} {r['total_return']:>+7.1f}%  "
              f"{r['trades']}笔 胜率{r['win_rate']:.0f}%")

    print(f"\n  BOTTOM 10:")
    for r in results[-10:]:
        print(f"    {r['code']} {r['name']:<20s} {r['total_return']:>+7.1f}%  "
              f"{r['trades']}笔 胜率{r['win_rate']:.0f}%")

    # 看看哪些类型表现好
    print(f"\n=== 分类表现 ===")
    categories = {
        "宽基": ["沪深300", "中证500", "中证1000", "中证2000", "上证50", "创业板", "科创50", "A500"],
        "行业-半导体": ["芯片", "半导体"],
        "行业-证券": ["证券", "券商"],
        "行业-医药": ["医药", "医疗", "创新药", "中药"],
        "行业-消费": ["酒", "消费", "食品", "家电"],
        "行业-TMT": ["通信", "5G", "计算机", "传媒", "游戏", "人工智能"],
        "行业-能源": ["煤炭", "电力", "新能源", "光伏", "有色", "稀土", "钢铁"],
        "行业-其他": ["银行", "军工", "地产", "汽车", "机械", "红利"],
        "跨境": ["港股", "恒生", "纳指", "中概", "日经", "标普"],
        "商品": ["黄金"],
    }
    for cat_name, keywords in categories.items():
        cat_results = [r for r in results if any(kw in r["name"] for kw in keywords)]
        if cat_results:
            avg = sum(r["total_return"] for r in cat_results) / len(cat_results)
            pos = sum(1 for r in cat_results if r["total_return"] > 0)
            print(f"  {cat_name}: {len(cat_results)}只, 平均{avg:+.1f}%, 正收益{pos}/{len(cat_results)}")


if __name__ == "__main__":
    main()
