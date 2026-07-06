# -*- coding: utf-8 -*-
"""
概念因子实验室 — 在 375 个同花顺概念板块上运行 27 个 Alpha 因子轮动回测。

与 ETF factor lab 不同，概念指数不可直接交易，回测为模拟持有：
每日按因子分数排 Top N 概念，等权分配，周期调仓。纯分析工具，不产生交易。

Usage:
  python script/concept_factor_lab.py --list
  python script/concept_factor_lab.py --factor alpha040
  python script/concept_factor_lab.py
  python script/concept_factor_lab.py --max-positions 10 --rebalance-days 10
"""

from __future__ import annotations

import argparse
import csv
import glob as _glob
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from models.stock import Concept, ConceptDaily
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 复用 alpha_factor_lab 的全部因子和工具函数
from script.alpha_factor_lab import (
    FACTOR_SPECS,
    FACTOR_BY_ID,
    _sma,
    _rolling_sum,
    _rolling_high,
    _rolling_low,
    _rolling_std,
    _rolling_corr,
    _rolling_cov,
    _delta,
    _ts_rank,
    _safe_div,
    _bar_arrays,
    _build_score_maps,
    _max_drawdown,
    _profit_factor,
)

# ======== 配置 ========

DEFAULT_START_DATE = "2022-05-06"
DEFAULT_INITIAL_CASH = 1_000_000.0
DEFAULT_MAX_POSITIONS = 10
DEFAULT_REBALANCE_DAYS = 5
DEFAULT_MAX_HOLD_DAYS = 40
DEFAULT_MIN_CONCEPTS_PER_DAY = 300
DEFAULT_DB_PATH = ROOT_DIR / "data" / "stock.db"
DEFAULT_CONCEPT_CSV_DIR = ROOT_DIR / "data" / "concept"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "factor_lab"

# ======== 数据加载 ========


def load_concept_bars_from_db(start_date=None, end_date=None):
    """从 SQLite 加载概念指数日线，返回 (stocks, bars_by_code)。"""
    engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}", echo=False)
    Session = sessionmaker(bind=engine)

    stocks = []
    bars_by_code = {}

    with Session() as sess:
        concepts = {c.code: c.name for c in sess.query(Concept).all()}

        query = sess.query(ConceptDaily).filter(ConceptDaily.volume > 0)
        if start_date:
            query = query.filter(ConceptDaily.trade_date >= start_date)
        if end_date:
            query = query.filter(ConceptDaily.trade_date <= end_date)
        rows = query.order_by(ConceptDaily.concept_code, ConceptDaily.trade_date).all()

        from collections import defaultdict

        grouped = defaultdict(list)
        for r in rows:
            grouped[r.concept_code].append({
                "trade_date": r.trade_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "amount": r.amount,
            })

        for code, bars in grouped.items():
            name = concepts.get(code, code)
            bars.sort(key=lambda b: b["trade_date"])
            stocks.append({
                "code": code,
                "name": name,
                "daily_count": len(bars),
                "latest_trade_date": bars[-1]["trade_date"],
            })
            bars_by_code[code] = bars

    stocks.sort(key=lambda s: s["code"])
    return stocks, bars_by_code


def load_concept_bars_from_files(start_date=None, end_date=None):
    """从 data/concept/{year}/YYYY-MM.csv 加载概念指数日线。

    文件格式（与 update_concept_ths.py 输出一致）:
        concept_code, concept_name, trade_date, open, high, low, close, volume, amount

    返回格式与 load_concept_bars_from_db 相同。
    """
    stocks = []
    bars_by_code = {}
    name_map = {}

    # 收集所有匹配的月度文件
    csv_dir = str(DEFAULT_CONCEPT_CSV_DIR)
    pattern = os.path.join(csv_dir, "*", "*.csv")
    csv_files = _glob.glob(pattern)

    from collections import defaultdict
    grouped = defaultdict(list)

    for fp in csv_files:
        # 从文件名提取月份: data/concept/2026/2026-06.csv → 2026-06
        basename = os.path.basename(fp)
        file_month = basename.replace(".csv", "")  # "2026-06"
        if not file_month[:4].isdigit():
            continue

        # 日期过滤
        if start_date and file_month < start_date[:7]:
            continue
        if end_date and file_month > end_date[:7]:
            continue

        # 读取 CSV
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                td = row.get("trade_date", "")
                if start_date and td < start_date:
                    continue
                if end_date and td > end_date:
                    continue

                code = row.get("concept_code", "")
                name = row.get("concept_name", "")
                if name:
                    name_map[code] = name

                vol_raw = row.get("volume")
                amt_raw = row.get("amount")
                grouped[code].append({
                    "trade_date": td,
                    "open": float(row["open"]) if row.get("open") else None,
                    "high": float(row["high"]) if row.get("high") else None,
                    "low": float(row["low"]) if row.get("low") else None,
                    "close": float(row["close"]) if row.get("close") else None,
                    "volume": int(float(vol_raw)) if vol_raw and vol_raw != "None" else 0,
                    "amount": float(amt_raw) if amt_raw and amt_raw != "None" else 0,
                })

    for code, bars in grouped.items():
        bars.sort(key=lambda b: b["trade_date"])
        name = name_map.get(code, code)
        stocks.append({
            "code": code,
            "name": name,
            "daily_count": len(bars),
            "latest_trade_date": bars[-1]["trade_date"],
        })
        bars_by_code[code] = bars

    stocks.sort(key=lambda s: s["code"])
    return stocks, bars_by_code


def load_concept_bars(start_date=None, end_date=None, source="auto"):
    """加载概念日线数据，自动选择数据源。

    参数:
        source: "auto"=优先CSV文件, "db"=数据库, "file"=CSV文件
    """
    if source == "db":
        return load_concept_bars_from_db(start_date, end_date)

    if source == "file":
        return load_concept_bars_from_files(start_date, end_date)

    # auto: 优先 CSV 文件，fallback 到数据库
    csv_dir = str(DEFAULT_CONCEPT_CSV_DIR)
    if os.path.isdir(csv_dir) and any(
        f.endswith(".csv") for f in os.listdir(csv_dir) if f[:4].isdigit()
    ):
        return load_concept_bars_from_files(start_date, end_date)

    return load_concept_bars_from_db(start_date, end_date)


# ======== 概念因子轮动回测 ========


def run_concept_factor_backtest(
    factor,
    stocks,
    bars_by_code,
    initial_cash=DEFAULT_INITIAL_CASH,
    max_positions=DEFAULT_MAX_POSITIONS,
    rebalance_days=DEFAULT_REBALANCE_DAYS,
    max_hold_days=DEFAULT_MAX_HOLD_DAYS,
    min_concepts_per_day=DEFAULT_MIN_CONCEPTS_PER_DAY,
):
    """对单个因子在概念池上做模拟轮动回测。

    每日按因子分数排序，持有 Top N 概念（等权），周期调仓。
    概念指数不可交易，以收盘价模拟持仓价值。

    返回:
        {summary, equity_curve, trades, current_positions}
    """
    stock_map = {s["code"]: s for s in stocks}
    dates, score_by_code_date, price_by_code_date = _build_score_maps(bars_by_code, factor)
    bar_lookup = {
        code: {b["trade_date"]: b for b in bars}
        for code, bars in bars_by_code.items()
    }

    # 过滤数据不完整的交易日（概念数不足则跳过调仓）
    daily_concept_count = {}
    for code, scores in score_by_code_date.items():
        for date in scores:
            daily_concept_count[date] = daily_concept_count.get(date, 0) + 1

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    rebalance_index = 0

    for date in dates:
        # 计算当日权益
        equity = cash
        for code, pos in positions.items():
            price = price_by_code_date.get(code, {}).get(date, pos["buy_price"])
            equity += pos["shares"] * price
        equity_curve.append({
            "date": date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_count": len(positions),
        })

        # 概念数不足则跳过调仓（只记录权益）
        if daily_concept_count.get(date, 0) < min_concepts_per_day:
            continue

        if rebalance_index % rebalance_days != 0:
            rebalance_index += 1
            continue
        rebalance_index += 1

        # 构建候选列表：当日有有效分数的所有概念
        candidates = []
        for code, scores in score_by_code_date.items():
            score = scores.get(date)
            bar = bar_lookup.get(code, {}).get(date)
            if score is None or not bar:
                continue
            candidates.append((score, code, bar))
        candidates.sort(reverse=True, key=lambda x: x[0])
        target_codes = {code for _, code, _ in candidates[:max_positions]}

        # 卖出：掉出 Top N 或超期持有
        for code in list(positions):
            pos = positions[code]
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            hold_days = pos["hold_days"] + rebalance_days
            sell_reason = None
            if code not in target_codes:
                sell_reason = f"rank_out({factor.id})"
            elif hold_days >= max_hold_days:
                sell_reason = f"max_hold_{max_hold_days}d"
            if sell_reason is None:
                pos["hold_days"] = hold_days
                continue
            sell_price = float(bar["close"])
            income = pos["shares"] * sell_price
            cost = pos["shares"] * pos["buy_price"]
            cash += income
            trades.append({
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "sell_date": date,
                "sell_price": round(sell_price, 3),
                "shares": pos["shares"],
                "profit": round(income - cost, 2),
                "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
                "buy_reason": pos["buy_reason"],
                "sell_reason": sell_reason,
            })
            del positions[code]

        # 买入：等权分配剩余现金
        for score, code, bar in candidates[:max_positions]:
            if code in positions or len(positions) >= max_positions:
                continue
            remaining_slots = max(1, max_positions - len(positions))
            budget = cash / remaining_slots
            price = float(bar["close"])
            shares = int(budget // price)
            if shares <= 0:
                continue
            cost = shares * price
            cash -= cost
            positions[code] = {
                "buy_date": date,
                "buy_price": price,
                "shares": shares,
                "hold_days": 0,
                "buy_reason": f"{factor.id} score={score:.4f}",
            }

    # 期末强制平仓
    last_date = dates[-1] if dates else None
    for code, pos in positions.items():
        sell_price = price_by_code_date.get(code, {}).get(last_date, pos["buy_price"])
        income = pos["shares"] * sell_price
        cost = pos["shares"] * pos["buy_price"]
        trades.append({
            "code": code,
            "name": stock_map.get(code, {"name": code})["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(sell_price, 3),
            "shares": pos["shares"],
            "profit": round(income - cost, 2),
            "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    # 汇总统计
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    closed_trades = [t for t in trades if t["sell_reason"] != "期末持仓"]
    wins = [t for t in closed_trades if t["profit"] > 0]
    avg_profit_pct = (
        sum(t["profit_pct"] for t in closed_trades) / len(closed_trades)
        if closed_trades else 0.0
    )
    summary = {
        "factor_id": factor.id,
        "factor_name": factor.name,
        "description": factor.description,
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
        "max_drawdown_pct": round(_max_drawdown([x["equity"] for x in equity_curve]), 2),
        "trade_count": len(closed_trades),
        "win_rate_pct": round(len(wins) / max(1, len(closed_trades)) * 100, 2),
        "avg_profit_pct": round(avg_profit_pct, 2),
        "profit_factor": _profit_factor(closed_trades),
        "open_positions": len(positions),
    }
    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "current_positions": [
            {
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "cur_price": round(price_by_code_date.get(code, {}).get(last_date, pos["buy_price"]), 3),
                "shares": pos["shares"],
            }
            for code, pos in positions.items()
        ],
    }


# ======== 输出 ========


def _save_results(results, args):
    """保存回测结果为 JSON + CSV。"""
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DEFAULT_OUTPUT_DIR / f"concept_factor_lab_{stamp}.json"
    csv_path = DEFAULT_OUTPUT_DIR / f"concept_factor_lab_{stamp}.csv"
    payload = {
        "params": vars(args),
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        fieldnames = [
            "factor_id", "factor_name", "total_return_pct", "max_drawdown_pct",
            "win_rate_pct", "trade_count", "avg_profit_pct", "profit_factor",
            "final_equity", "open_positions",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            writer.writerow({k: item["summary"].get(k) for k in fieldnames})
    return json_path, csv_path


# ======== CLI ========


def parse_args():
    parser = argparse.ArgumentParser(description="概念因子轮动回测实验室")
    parser.add_argument("--factor", default="all", help="因子 ID，逗号分隔或 all")
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH)
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help=f"持有概念数 (默认 {DEFAULT_MAX_POSITIONS})")
    parser.add_argument("--rebalance-days", type=int, default=DEFAULT_REBALANCE_DAYS,
                        help=f"调仓周期 (默认 {DEFAULT_REBALANCE_DAYS})")
    parser.add_argument("--max-hold-days", type=int, default=DEFAULT_MAX_HOLD_DAYS,
                        help=f"最大持有天数 (默认 {DEFAULT_MAX_HOLD_DAYS})")
    parser.add_argument("--min-concepts", type=int, default=DEFAULT_MIN_CONCEPTS_PER_DAY,
                        help=f"当日最少概念数，不足则跳过调仓 (默认 {DEFAULT_MIN_CONCEPTS_PER_DAY})")
    parser.add_argument("--list", action="store_true", help="列出可用因子")
    parser.add_argument("--no-save", action="store_true", help="不保存 JSON/CSV")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list:
        print(f"{'ID':12s} {'名称':18s} {'描述'}")
        print("-" * 70)
        for factor in FACTOR_SPECS:
            print(f"{factor.id:12s} {factor.name:18s} {factor.description}")
        return

    # 解析因子列表
    if args.factor == "all":
        factors = FACTOR_SPECS
    else:
        factors = []
        for factor_id in [x.strip() for x in args.factor.split(",") if x.strip()]:
            if factor_id not in FACTOR_BY_ID:
                raise SystemExit(f"未知因子: {factor_id}")
            factors.append(FACTOR_BY_ID[factor_id])

    # 加载数据
    stocks, bars_by_code = load_concept_bars(start_date=args.start, end_date=args.end)
    print(f"加载概念: {len(stocks)} 个, 日期范围: {args.start} ~ {args.end or '最新'}")

    # 计算有效交易日数
    from collections import Counter
    date_counter = Counter()
    for bars in bars_by_code.values():
        for b in bars:
            date_counter[b["trade_date"]] += 1
    good_days = sum(1 for d, c in date_counter.items() if c >= args.min_concepts)
    max_concepts = max(date_counter.values()) if date_counter else 0
    print(f"有效交易日: {good_days} 天 (≥{args.min_concepts} 概念), 单日最多: {max_concepts} 概念")
    print()

    # 运行回测
    header = (
        f"{'factor':10s} {'return':>9s} {'dd':>7s} {'win':>7s} "
        f"{'trades':>7s} {'avg':>7s} {'pf':>7s} {'open':>5s}"
    )
    print(header)
    print("-" * 72)

    results = []
    for factor in factors:
        result = run_concept_factor_backtest(
            factor,
            stocks,
            bars_by_code,
            initial_cash=args.initial_cash,
            max_positions=args.max_positions,
            rebalance_days=args.rebalance_days,
            max_hold_days=args.max_hold_days,
            min_concepts_per_day=args.min_concepts,
        )
        results.append(result)
        s = result["summary"]
        pf = s["profit_factor"]
        pf_text = "None" if pf is None else f"{pf:.2f}"
        print(
            f"{s['factor_id']:10s} {s['total_return_pct']:>+8.2f}% "
            f"{s['max_drawdown_pct']:>6.2f}% {s['win_rate_pct']:>6.2f}% "
            f"{s['trade_count']:>7d} {s['avg_profit_pct']:>+6.2f}% "
            f"{pf_text:>7s} {s['open_positions']:>5d}"
        )

    # 排序输出
    results.sort(
        key=lambda r: (
            r["summary"]["profit_factor"] or 0,
            r["summary"]["total_return_pct"],
        ),
        reverse=True,
    )

    if not args.no_save:
        json_path, csv_path = _save_results(results, args)
        print(f"\n已保存: {json_path}")
        print(f"已保存: {csv_path}")

    # 打印 Top 5
    print(f"\n======== Top 5 (按 PF) ========")
    for i, r in enumerate(results[:5], 1):
        s = r["summary"]
        pf = s["profit_factor"]
        pf_text = "N/A" if pf is None else f"{pf:.2f}"
        print(f"  {i}. {s['factor_id']:12s} {s['factor_name']:18s}  "
              f"return={s['total_return_pct']:>+7.2f}%  dd={s['max_drawdown_pct']:>5.2f}%  "
              f"PF={pf_text}  win={s['win_rate_pct']:.1f}%")


if __name__ == "__main__":
    main()
