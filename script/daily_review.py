#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日回顾报告 — 汇总所有策略信号 + 持仓 + 大盘评估。

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

        # ======== 6. 最近操作（最近5天） ========
        all_dates = sorted(set(t.get("date", "") for t in trade_log), reverse=True)
        recent_dates = all_dates[:5]
        recent_trades = [t for t in trade_log if t.get("date") in recent_dates]

        # ======== 构建报告 ========
        signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
        lines = []
        lines.append(f"# 每日回顾 {_weekday_str(latest)}")
        lines.append(f"")
        lines.append(f"> 生成时间: {now_str}")
        lines.append(f"")

        # -- 大盘环境 --
        lines.append(f"## 大盘环境")
        lines.append(f"")
        sig = market.get("signal", "RED")
        lines.append(f"- **信号灯**: {signal_label.get(sig, sig)} (评分 {market.get('composite_score', '-')})")
        lines.append(f"- 涨跌比: {market.get('breadth_current', '-')} | 跌停数: {market.get('limit_down_current', '-')} | 成交额: {market.get('amount_current', 0)/1e8:.0f}亿")
        lines.append(f"- 评估: {market.get('summary', '-')}")
        lines.append(f"")

        # -- market_bottom 策略 --
        lines.append(f"## 策略信号")
        lines.append(f"")
        lines.append(f"### market_bottom（大底抄底）")
        lines.append(f"")
        if mb_result:
            lines.append(f"- 回测区间: {mb_result.get('selection', {}).get('start_date', '-')} ~ {mb_result.get('selection', {}).get('end_date', '-')}")
            lines.append(f"- 累计收益: {mb_summary.get('total_return_pct', '-')}% | 最大回撤: {mb_summary.get('max_drawdown_pct', '-')}% | 胜率: {mb_summary.get('win_rate_pct', '-')}%")
            lines.append(f"- 总交易: {mb_summary.get('trade_count', 0)}笔 | 盈亏比: {mb_summary.get('profit_factor', '-')}")
        lines.append(f"")

        if mb_buys_today:
            lines.append(f"**今日买入信号 ({len(mb_buys_today)}只):**")
            for t in mb_buys_today:
                lines.append(f"- {t['name']}({t['code']}) {t.get('buy_price', '-')}元 | {t.get('buy_reason', '')}")
            lines.append(f"")

        if mb_sells_today:
            lines.append(f"**今日卖出信号 ({len(mb_sells_today)}只):**")
            for t in mb_sells_today:
                sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                lines.append(f"- {t['name']}({t['code']}) 盈亏 {sign}{t.get('profit_pct', 0):.1f}% → {t.get('sell_reason', '')}")
            lines.append(f"")

        if not mb_buys_today and not mb_sells_today:
            lines.append(f"今日无买卖信号")
            lines.append(f"")

        if mb_open:
            lines.append(f"**当前回测持仓 ({len(mb_open)}只):**")
            for t in mb_open[:10]:
                sign = "+" if t.get("profit_pct", 0) >= 0 else ""
                lines.append(f"- {t['name']}({t['code']}) 买入 {t.get('buy_date', '')} | {sign}{t.get('profit_pct', 0):.1f}%")
            if len(mb_open) > 10:
                lines.append(f"- ... 还有 {len(mb_open) - 10} 只")
            lines.append(f"")

        # -- V反候选 --
        lines.append(f"### V反候选（daily_guide）")
        lines.append(f"")
        if v_reversal_candidates:
            strong = [c for c in v_reversal_candidates if c["simple_score"] >= 45]
            watch = [c for c in v_reversal_candidates if 35 <= c["simple_score"] < 45]
            lines.append(f"- 强信号(≥45分): {len(strong)}只 | 可关注(35-45分): {len(watch)}只 | 总计: {len(v_reversal_candidates)}只")
            lines.append(f"")
            if strong[:5]:
                lines.append(f"**强信号前5:**")
                for c in strong[:5]:
                    lines.append(f"- {c['name']}({c['code']}) {c['v_label']} | 评分{c['simple_score']:.0f}")
                lines.append(f"")
        else:
            lines.append(f"无候选（或数据不足）")
            lines.append(f"")

        # -- 实盘持仓 --
        lines.append(f"## 实盘持仓")
        lines.append(f"")
        lines.append(f"现金: {cash:,.0f} | 持仓市值: {total_market_value:,.0f} | 总资产: {total_assets:,.0f}")
        lines.append(f"")
        if holdings:
            lines.append(f"| 股票 | 成本 | 现价 | 盈亏% | 市值 | 持仓天 | 来源 |")
            lines.append(f"|------|------|------|-------|------|--------|------|")
            for h in holdings:
                name = h["name"]
                cost = f"{h['buy_price']:.2f}"
                if h["current_price"]:
                    cur = f"{h['current_price']:.2f}"
                else:
                    cur = "-"
                if h["pnl_pct"] is not None:
                    sign = "+" if h["pnl_pct"] >= 0 else ""
                    pnl = f"{sign}{h['pnl_pct']:.1f}%"
                else:
                    pnl = "-"
                if h["market_value"]:
                    mv = f"{h['market_value']:,.0f}"
                else:
                    mv = "-"
                # 持仓天数
                if h["buy_date"]:
                    try:
                        bd = datetime.strptime(h["buy_date"], "%Y-%m-%d")
                        ld = datetime.strptime(latest, "%Y-%m-%d")
                        days = (ld - bd).days
                    except:
                        days = "-"
                else:
                    days = "-"
                src = h.get("strategy_source", "") or "-"
                lines.append(f"| {name} | {cost} | {cur} | {pnl} | {mv} | {days} | {src} |")
        else:
            lines.append(f"(空仓)")
        lines.append(f"")

        # -- 今日操作 --
        lines.append(f"## 今日操作")
        lines.append(f"")
        if today_buys:
            lines.append(f"**买入:**")
            for t in today_buys:
                lines.append(f"- {t.get('name', t.get('code'))} {t['price']:.2f}元 × {t['shares']}股 = {t.get('amount', 0):,.0f}")
                if t.get("reason"):
                    lines.append(f"  理由: {t['reason']}")
        else:
            lines.append(f"买入: 无")
        lines.append(f"")

        if today_sells:
            lines.append(f"**卖出:**")
            for t in today_sells:
                lines.append(f"- {t.get('name', t.get('code'))} {t['price']:.2f}元 × {t['shares']}股")
                if t.get("reason"):
                    lines.append(f"  理由: {t['reason']}")
        else:
            lines.append(f"卖出: 无")
        lines.append(f"")

        # -- 操作建议 --
        lines.append(f"## 决策建议")
        lines.append(f"")
        if sig == "GREEN":
            lines.append(f"- 大盘 GREEN，可以积极参与")
            lines.append(f"- 重点参考 market_bottom 买入信号 + V反强信号候选")
        elif sig == "YELLOW":
            lines.append(f"- 大盘 YELLOW，控制仓位在 50% 以内")
            lines.append(f"- 精选 market_bottom 信号中跌幅最深的 1-2 只")
        else:
            lines.append(f"- 大盘 RED，建议观望，不新开仓")
            lines.append(f"- 检查现有持仓的止损线，做好风控")

        if mb_buys_today:
            lines.append(f"- 明日重点关注: {', '.join(t['name'] for t in mb_buys_today[:3])}（market_bottom信号）")
        if v_reversal_candidates:
            top_v = [c for c in v_reversal_candidates if c["simple_score"] >= 45][:3]
            if top_v:
                lines.append(f"- V反强信号: {', '.join(c['name'] for c in top_v)}")

        lines.append(f"")

        # -- 最近操作记录 --
        if recent_trades:
            lines.append(f"## 最近操作")
            lines.append(f"")
            for t in recent_trades[:10]:
                act = "[买]" if t.get("action") == "buy" else "[卖]"
                lines.append(f"- {t.get('date', '')} {act} {t.get('name', t.get('code'))} {t.get('price', '-')}元 × {t.get('shares', '-')}股")
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
    parser = argparse.ArgumentParser(description="每日回顾报告")
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
    print("  每日回顾报告 — 生成中...")
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
