# -*- coding: utf-8 -*-
"""
统一回测入口 — 支持 concept / etf / stock 三种标的池

用法:
  python script/run_backtest.py --universe concept --strategy divergent_adaptive
  python script/run_backtest.py --universe etf --strategy etf_alpha
  python script/run_backtest.py --universe stock --strategy alpha042
  python script/run_backtest.py --universe concept --strategy all
  python script/run_backtest.py --list               # 列出所有策略

通用参数:
  --start 2023-01-01    起始日期
  --cash 1000000        初始资金
  --max-positions 5     最大持仓数
"""

import argparse, csv, glob, json, os, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from backtest import get_strategy, run_portfolio_backtest
from backtest.registry import list_strategies
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import Base, StockDaily, StockInfo

# ═══════════════════════════════════════════════════════════
# 默认参数
# ═══════════════════════════════════════════════════════════

ARCHIVE_ROOT = ROOT_DIR / "data" / "strategy"
CONCEPT_DATA_DIR = ROOT_DIR / "data" / "concept"
ETF_DATA_DIR = ROOT_DIR / "data" / "etf"

DEFAULT_START_DATE = "2023-01-01"
DEFAULT_CASH = 1_000_000
DEFAULT_MAX_POSITIONS = 5
MIN_BAR_COUNT = 120


# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════

def load_concept_bars(start_date=None):
    """从 data/concept/ 加载概念日线。"""
    csv_files = glob.glob(str(CONCEPT_DATA_DIR / "*" / "*.csv"))
    grouped = defaultdict(list)
    name_map = {}

    for fp in csv_files:
        if not os.path.basename(fp)[:4].isdigit():
            continue
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row.get("concept_code", "")
                name = row.get("concept_name", "")
                if name: name_map[code] = name
                close = float(row["close"]) if row.get("close") and row["close"] != "None" else None
                if close is None: continue
                vol_v = row.get("volume"); amt_v = row.get("amount")
                grouped[code].append({
                    "trade_date": row.get("trade_date", ""),
                    "open": float(row["open"]) if row.get("open") and row["open"] != "None" else close,
                    "high": float(row["high"]) if row.get("high") and row["high"] != "None" else close,
                    "low": float(row["low"]) if row.get("low") and row["low"] != "None" else close,
                    "close": close,
                    "volume": int(float(vol_v)) if vol_v and vol_v != "None" else 0,
                    "amount": float(amt_v) if amt_v and amt_v != "None" else 0,
                })

    bars_by_code = {}
    for code, bars in grouped.items():
        bars.sort(key=lambda b: b["trade_date"])
        if len(bars) >= MIN_BAR_COUNT:
            bars_by_code[code] = bars  # 保留全部历史，策略内部自行过滤日期

    stock_map = {code: {"code": code, "name": name_map.get(code, code), "market": "cn"}
                 for code in bars_by_code}
    return bars_by_code, stock_map


def load_etf_bars(start_date=None):
    """从 data/etf/ 加载 ETF 日线。"""
    csv_files = glob.glob(str(ETF_DATA_DIR / "*" / "*.csv"))
    grouped = defaultdict(list)
    name_map = {}

    # 排除海外 ETF
    blacklist = ["纳指", "港股", "恒生", "中概", "标普", "道琼",
                 "德国", "日经", "法国", "印度", "越南", "韩国"]

    for fp in csv_files:
        if not os.path.basename(fp)[:4].isdigit(): continue
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row.get("code", "")
                name = row.get("name", "")
                if any(kw in name for kw in blacklist): continue
                if name: name_map[code] = name
                close = float(row["close"]) if row.get("close") and row["close"] != "None" else None
                if close is None: continue
                vol_v = row.get("volume"); amt_v = row.get("amount")
                grouped[code].append({
                    "trade_date": row.get("trade_date", ""),
                    "open": float(row["open"]) if row.get("open") and row["open"] != "None" else close,
                    "high": float(row["high"]) if row.get("high") and row["high"] != "None" else close,
                    "low": float(row["low"]) if row.get("low") and row["low"] != "None" else close,
                    "close": close,
                    "volume": int(float(vol_v)) if vol_v and vol_v != "None" else 0,
                    "amount": float(amt_v) if amt_v and amt_v != "None" else 0,
                })

    bars_by_code = {}
    for code, bars in grouped.items():
        bars.sort(key=lambda b: b["trade_date"])
        if len(bars) >= MIN_BAR_COUNT:
            bars_by_code[code] = bars

    stock_map = {code: {"code": code, "name": name_map.get(code, code), "market": "cn"}
                 for code in bars_by_code}
    return bars_by_code, stock_map


def load_stock_bars(start_date=None):
    """从 SQLite 加载 A 股日线。"""
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        latest = sess.query(StockDaily.trade_date).order_by(
            StockDaily.trade_date.desc()).first()
        if not latest:
            return {}, {}
        latest_date = latest[0]
        effective_start = start_date or DEFAULT_START_DATE

        # 活跃股票
        active = (
            sess.query(StockInfo.code, StockInfo.name, StockInfo.market)
            .join(StockDaily, StockInfo.code == StockDaily.code)
            .filter(StockInfo.type == "1", StockInfo.status == 1,
                    StockDaily.trade_date == latest_date)
            .all()
        )
        stock_map = {s.code: {"code": s.code, "name": s.name, "market": s.market}
                     for s in active}

        # K线
        rows = (
            sess.query(StockDaily)
            .filter(StockDaily.code.in_(stock_map.keys()),
                    StockDaily.trade_date >= effective_start)
            .order_by(StockDaily.code, StockDaily.trade_date)
            .all()
        )

        bars_by_code = defaultdict(list)
        for r in rows:
            bars_by_code[r.code].append({
                "trade_date": r.trade_date, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close,
                "volume": r.volume or 0, "amount": r.amount or 0,
            })

        # 过滤数据不足的
        clean_bars = {}
        clean_map = {}
        for code, bars in bars_by_code.items():
            if len(bars) >= MIN_BAR_COUNT:
                clean_bars[code] = bars
                clean_map[code] = stock_map[code]

        return clean_bars, clean_map
    finally:
        sess.close()


# ═══════════════════════════════════════════════════════════
# 结果保存
# ═══════════════════════════════════════════════════════════

def save_result(strategy_id, result, stock_count, start_date, end_date,
                initial_cash, max_positions):
    """保存回测结果到 data/strategy/{id}/YYYY-MM/。"""
    trades = result["trades"]
    trades.sort(key=lambda x: (x["buy_date"], x["code"]))

    # 期末持仓
    current_positions = []
    for t in trades:
        if t.get("sell_reason") == "期末持仓":
            current_positions.append({
                "code": t["code"], "name": t.get("name", t["code"]),
                "buy_date": t["buy_date"], "buy_price": t["buy_price"],
                "cur_price": t["sell_price"], "shares": t["shares"],
                "cost": round(t["buy_amount"], 2) if "buy_amount" in t
                       else round(t["buy_price"] * t["shares"], 2),
                "market_value": round(t["sell_amount"], 2) if "sell_amount" in t
                                else round(t["sell_price"] * t["shares"], 2),
                "profit": t["profit"], "profit_pct": t["profit_pct"],
            })
    current_positions.sort(key=lambda x: x["profit"], reverse=True)

    # 标的统计
    stock_summaries = []
    by_code = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_sum": 0.0, "profit": 0.0})
    for t in trades:
        if t.get("sell_reason") == "期末持仓": continue
        c = t["code"]
        by_code[c]["trades"] += 1
        by_code[c]["name"] = t.get("name", c)
        by_code[c]["code"] = c
        by_code[c]["profit"] += t["profit"]
        if t["profit"] > 0: by_code[c]["wins"] += 1
        by_code[c]["pnl_sum"] += t["profit_pct"]

    for c, st in by_code.items():
        stock_summaries.append({
            "code": c, "name": st["name"],
            "trade_count": st["trades"],
            "win_rate": round(st["wins"] / st["trades"] * 100, 1),
            "total_profit": round(st["profit"], 2),
            "avg_profit_pct": round(st["pnl_sum"] / st["trades"], 2),
        })
    stock_summaries.sort(key=lambda x: -x["total_profit"])

    # 确保 trades 有 buy_amount/sell_amount
    for t in trades:
        if "buy_amount" not in t:
            t["buy_amount"] = round(t["buy_price"] * t["shares"], 2)
        if "sell_amount" not in t:
            t["sell_amount"] = round(t["sell_price"] * t["shares"], 2)
        if "buy_reason" not in t:
            t["buy_reason"] = ""

    latest_date = end_date or max(
        (t.get("sell_date", "") for t in trades), default=datetime.now().strftime("%Y-%m-%d"))
    year_month = latest_date[:7]

    archive_dir = ARCHIVE_ROOT / strategy_id / year_month
    archive_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"{latest_date}_"
    max_seq = 0
    for f in archive_dir.glob(f"{prefix}*.json"):
        try:
            seq = int(f.stem[len(prefix):])
            if seq > max_seq: max_seq = seq
        except ValueError: pass

    archive_path = archive_dir / f"{prefix}{max_seq + 1:02d}.json"

    # strategy meta
    try:
        strat = get_strategy(strategy_id)
        strat_meta = dict(strat.META)
    except Exception:
        strat_meta = {"id": strategy_id, "name": strategy_id,
                      "description": "", "type": "unknown"}

    output = {
        "strategy": strat_meta,
        "selection": {
            "stock_count": stock_count,
            "start_date": start_date,
            "end_date": latest_date,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_position_cash": round(initial_cash / max_positions, 2),
            "latest_trade_date": latest_date,
        },
        "summary": result["summary"],
        "equity_curve": result["equity_curve"],
        "stock_summaries": stock_summaries,
        "trades": trades,
        "current_positions": current_positions,
        "market_gate": result.get("market_gate"),
    }
    archive_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
    return archive_path


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="统一回测入口")
    parser.add_argument("--universe", choices=["concept", "etf", "stock"],
                        default="stock", help="标的池 (default: stock)")
    parser.add_argument("--strategy", default=None, help="策略 ID，或 'all' 跑全部")
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="起始日期")
    parser.add_argument("--end", default=None, help="结束日期")
    parser.add_argument("--cash", type=float, default=DEFAULT_CASH, help="初始资金")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help="最大持仓数")
    parser.add_argument("--list", action="store_true", help="列出全部策略")
    parser.add_argument("--top", type=int, default=10, help="打印前N个标的")
    args = parser.parse_args()

    # ---- 列出策略 ----
    if args.list:
        strategies = list_strategies()
        print(f"共 {len(strategies)} 个策略:\n")
        for meta in strategies:
            utype = meta.get("type", "stock")
            print(f"  {meta['id']:<30} [{utype:<8}] {meta['name']}")
        return

    if not args.strategy:
        parser.error("需要 --strategy 或 --list")

    # ---- 确定要跑的策略 ----
    all_meta = list_strategies()
    all_ids = {m["id"]: m for m in all_meta}

    if args.strategy == "all":
        strategy_ids = list(all_ids.keys())
    else:
        strategy_ids = [s.strip() for s in args.strategy.split(",")]

    # ---- 加载数据 ----
    print(f"[数据] 加载 {args.universe} 标的...")
    if args.universe == "concept":
        bars_by_code, stock_map = load_concept_bars(start_date=args.start)
    elif args.universe == "etf":
        bars_by_code, stock_map = load_etf_bars(start_date=args.start)
    else:
        bars_by_code, stock_map = load_stock_bars(start_date=args.start)

    if not bars_by_code:
        print("[错误] 没有可用标的")
        return

    # 获取最新日期
    all_dates = sorted(set(d for bars in bars_by_code.values()
                           for b in bars for d in [b["trade_date"]]))
    end_date = args.end or all_dates[-1]
    stock_count = len(bars_by_code)

    print(f"[??] {stock_count} ???, ???? {all_dates[0]} ~ {all_dates[-1]}")
    print(f"[??] ???? {args.start} ~ {end_date}")
    print()

    # ---- 逐个跑 ----
    for sid in strategy_ids:
        if sid not in all_ids:
            print(f"[跳过] {sid}: 未找到策略, 跳过")
            continue

        meta = all_ids[sid]
        print(f"[{meta['id']}] {meta['name']}")

        try:
            strategy = get_strategy(sid)
        except Exception as e:
            print(f"  [错误] 加载策略失败: {e}")
            continue

        # ---- 跨截面策略走独立引擎 ----
        if sid == "divergent_adaptive" and args.universe == "concept":
            from backtest.strategy.strategy_divergent_adaptive import run_adaptive_backtest
            name_map = {c: s["name"] for c, s in stock_map.items()}
            result = run_adaptive_backtest(bars_by_code, name_map,
                                           initial_cash=args.cash, start_date=args.start,
                                           end_date=args.end)
            # 转换为统一格式
            result["stock_summaries"] = []
            result["market_gate"] = [
                {"date": r["date"], "allowed": True, "state": r["regime"].upper(),
                 "reasons": [r["regime"]]}
                for r in result.get("regime_log", [])
            ]
        else:
            try:
                result = run_portfolio_backtest(
                    bars_by_code, stock_map, strategy,
                    initial_cash=args.cash, max_positions=args.max_positions,
                    start_date=args.start, end_date=args.end,
                )
            except Exception as e:
                print(f"  [错误] 回测失败: {e}")
                continue

        s = result["summary"]
        print(f"  收益: {s['total_return_pct']:+.2f}% | "
              f"回撤: {s['max_drawdown_pct']:.2f}% | "
              f"胜率: {s['win_rate_pct']:.1f}% | "
              f"PF: {s['profit_factor']} | "
              f"交易: {s['trade_count']}笔")

        # 保存
        archive_path = save_result(
            sid, result, stock_count, args.start, end_date,
            args.cash, args.max_positions,
        )
        print(f"  已保存: {archive_path}")

        # 打印 Top
        stock_ss = result.get("stock_summaries", [])
        if not stock_ss:
            from collections import defaultdict as _dd
            by_c = _dd(lambda: {"cnt": 0, "w": 0, "profit": 0.0, "name": "", "code": ""})
            for t in result["trades"]:
                if t.get("sell_reason") in ("期末", "期末持仓"): continue
                c = t["code"]
                by_c[c]["cnt"] += 1; by_c[c]["code"] = c
                by_c[c]["name"] = t.get("name", c)
                by_c[c]["profit"] += t.get("profit", 0)
                if t.get("profit", 0) > 0: by_c[c]["w"] += 1
            stock_ss = list(by_c.values())
        summaries = sorted(stock_ss, key=lambda x: -x.get("profit", 0))[:args.top]
        if summaries:
            print(f"  Top {min(args.top, len(summaries))} 标的:")
            for ss in summaries:
                cnt = ss.get("trade_count", ss.get("cnt", 1))
                w = ss.get("wins", ss.get("w", 0))
                wr = ss.get("win_rate", 0) or (w / max(1, cnt) * 100)
                profit = ss.get("total_profit", ss.get("profit", 0))
                print(f"    {ss['name']:<16} {cnt:>3}笔  "
                      f"胜率{wr:.0f}%  累计{profit:>+10.0f}")

        # 当前持仓
        positions = result.get("current_positions", [])
        if positions:
            print(f"  当前持仓 ({len(positions)}个):")
            for p in positions[:5]:
                print(f"    {p.get('name','?'):<16} 买{p['buy_date']} "
                      f"@{p['buy_price']:.2f}  现{p['cur_price']:.2f}  "
                      f"{p['profit_pct']:+.1f}%")

        print()

    print("Done.")


if __name__ == "__main__":
    main()
