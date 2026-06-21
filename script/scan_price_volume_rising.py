#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价增量增策略 — 每日扫描 + QQ推送

扫描全市场符合"价增量增"条件的股票，按综合评分排序，
推送最佳3只到QQ，供次日开盘参考。

用法:
  python script/scan_price_volume_rising.py              # 标准输出
  python script/scan_price_volume_rising.py --top 5      # 展示前N只
  python script/scan_price_volume_rising.py --push        # 推送到QQ
  python script/scan_price_volume_rising.py --json        # 输出JSON
"""
import sys
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# 修复 Windows GBK 终端编码问题
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
from backtest.indicators import sma
from backtest.portfolio import _build_market_stats
from backtest.strategy.strategy_price_volume_rising import (
    _compute_price_volume_dynamics,
    market_gate,
    PRICE_UP_5D_MIN,
    VOL_RATIO_BUY,
    VOL_RATIO_MAX,
    VOL_TREND_ACCEL,
    UP_VOL_RATIO,
    PRICE_ABOVE_MA10_MAX,
    MIN_CONSEC_UP,
    LOOKBACK_DAYS,
)

# ============ 可调参数 ============

# -- 评分权重 --
WEIGHT_PRICE = 0.30       # 价格趋势强度
WEIGHT_VOLUME = 0.30      # 量能放大强度
WEIGHT_COORD = 0.25       # 量价配合健康度
WEIGHT_QUALITY = 0.15     # 趋势质量（连涨、位置）

# -- 展示 --
DEFAULT_TOP_N = 5


# ============ 工具函数 ============

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _weekday_str(date_str: str) -> str:
    """在日期后附加星期几"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{date_str} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def _fmt_pct(val: float) -> str:
    """格式化百分比"""
    return f"{val * 100:+.1f}%"


# ============ Phase 1: 候选股扫描 ============

def _check_buy_conditions(closes, volumes, i, ma10, vol_ma5, vol_ma20):
    """
    在 index=i 处检查价增量增买入条件。

    满足全部条件返回候选股dict，否则返回 None。
    """
    if ma10[i] is None or vol_ma5[i] is None or vol_ma20[i] is None:
        return None
    if vol_ma20[i] == 0:
        return None

    close = closes[i]
    vol_ratio = vol_ma5[i] / vol_ma20[i]

    # 条件1: 5日涨幅
    if i < 5 or closes[i - 5] <= 0:
        return None
    chg_5d = (close - closes[i - 5]) / closes[i - 5]
    if chg_5d < PRICE_UP_5D_MIN:
        return None

    # 条件2: 站上 MA10
    if close <= ma10[i]:
        return None

    # 条件3: 量能放大
    if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
        return None

    # 条件4: 量能加速
    if i >= 6:
        recent_3 = sum(volumes[i - 2:i + 1]) / 3
        prior_3 = sum(volumes[i - 5:i - 2]) / 3
        if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
            return None

    # 条件5: 量价配合
    dyn = _compute_price_volume_dynamics(closes, volumes, i)
    if dyn["up_vol_ratio"] < UP_VOL_RATIO:
        return None

    # 条件6: 不追高
    if close > ma10[i] * PRICE_ABOVE_MA10_MAX:
        return None

    # 条件7: 连续上涨
    if dyn["consecutive_up"] < MIN_CONSEC_UP:
        return None

    # ---- 提取详细信息 ----
    # 10日涨幅
    chg_10d = 0
    if i >= 10 and closes[i - 10] > 0:
        chg_10d = (close - closes[i - 10]) / closes[i - 10]

    # 20日涨幅
    chg_20d = 0
    if i >= 20 and closes[i - 20] > 0:
        chg_20d = (close - closes[i - 20]) / closes[i - 20]

    # 量能趋势（近3日 / 前3日）
    vol_trend_ratio = 1.0
    if i >= 6:
        recent_3 = sum(volumes[i - 2:i + 1]) / 3
        prior_3 = sum(volumes[i - 5:i - 2]) / 3
        if prior_3 > 0:
            vol_trend_ratio = recent_3 / prior_3

    # 距 MA10 距离
    dist_from_ma10 = (close - ma10[i]) / ma10[i]

    return {
        "code": "",
        "name": "",
        "market": "",

        # 价格趋势
        "chg_5d": round(chg_5d, 5),
        "chg_10d": round(chg_10d, 5),
        "chg_20d": round(chg_20d, 5),
        "close": close,
        "ma10": round(ma10[i], 2),
        "dist_from_ma10": round(dist_from_ma10, 4),

        # 量能
        "vol_ratio": round(vol_ratio, 2),
        "vol_ma5": round(vol_ma5[i], 0),
        "vol_ma20": round(vol_ma20[i], 0),
        "vol_trend_ratio": round(vol_trend_ratio, 2),

        # 量价配合
        "up_vol_ratio": dyn["up_vol_ratio"],
        "up_days": dyn["up_days"],
        "down_days": dyn["down_days"],
        "consecutive_up": dyn["consecutive_up"],

        # 今日涨跌幅
        "daily_chg": round((closes[i] - closes[i - 1]) / closes[i - 1], 5)
        if i > 0 and closes[i - 1] > 0 else 0,

        # 评分（Phase 2填入）
        "score_price": 0.0,
        "score_volume": 0.0,
        "score_coordination": 0.0,
        "score_quality": 0.0,
        "score": 0.0,
        "rank": 0,
    }


def screen_candidates(bars_by_code: dict, stock_map: dict, latest_date: str) -> list[dict]:
    """扫描所有股票，找出最新一天满足买入条件的候选股。"""
    candidates = []
    total_scanned = 0
    total_skipped = 0

    for code, bars in bars_by_code.items():
        if len(bars) < 25:
            total_skipped += 1
            continue
        total_scanned += 1

        if bars[-1]["trade_date"] != latest_date:
            continue

        closes = [b["close"] for b in bars]
        volumes = [b.get("volume") or 0 for b in bars]

        ma10 = sma(closes, 10)
        vol_ma5 = sma(volumes, 5)
        vol_ma20 = sma(volumes, 20)
        idx = len(closes) - 1

        result = _check_buy_conditions(closes, volumes, idx, ma10, vol_ma5, vol_ma20)
        if result is None:
            continue

        stock = stock_map.get(code, {"code": code, "name": code, "market": ""})
        result["code"] = code
        result["name"] = stock.get("name", code)
        result["market"] = stock.get("market", "")
        candidates.append(result)

    print(f"  扫描: {total_scanned} 只 | 跳过(<25天): {total_skipped} 只 | 候选: {len(candidates)} 只")
    return candidates


# ============ Phase 2: 评分排序 ============

def _score_price(c: dict) -> float:
    """
    价格趋势强度评分 (0-100)。

    5日涨幅映射: 1%→0, 10%→100（线性）
    10日20日趋势作为加分项
    """
    chg_5d = c["chg_5d"] * 100
    base = (chg_5d - PRICE_UP_5D_MIN * 100) / (10 - PRICE_UP_5D_MIN * 100) * 80
    base = _clamp(base, 0, 80)

    # 中长期趋势加分
    if c["chg_10d"] > 0.02:
        base += 10
    if c["chg_20d"] > 0:
        base += 10

    return _clamp(base, 0, 100)


def _score_volume(c: dict) -> float:
    """
    量能放大强度评分 (0-100)。

    vol_ratio 映射: 1.5→0, 3.5→100
    过高的 vol_ratio（>3.5）反而降分（异常爆量不可持续）
    """
    vr = c["vol_ratio"]
    if vr <= 3.5:
        score = (vr - VOL_RATIO_BUY) / (3.5 - VOL_RATIO_BUY) * 100
    else:
        # 超过 3.5 线性降分，到 4.0 降到 60
        score = 100 - (vr - 3.5) / 0.5 * 40

    # 量能加速加分
    if c["vol_trend_ratio"] > 1.2:
        score += 10

    return _clamp(score, 0, 100)


def _score_coordination(c: dict) -> float:
    """
    量价配合健康度评分 (0-100)。

    up_vol_ratio 映射: 1.2→0, 2.5→100
    上涨放量越多说明资金越认可
    """
    uvr = c["up_vol_ratio"]
    score = (uvr - UP_VOL_RATIO) / (2.5 - UP_VOL_RATIO) * 100

    # 阳线天数多加分
    if c["up_days"] >= 7:
        score += 10

    return _clamp(score, 0, 100)


def _score_quality(c: dict) -> float:
    """
    趋势质量评分 (0-100)。

    连续上涨天数 + 距离MA10不要太远(不追高)
    """
    # 连涨天数: 2→0, 5+→100
    cu = c["consecutive_up"]
    cons_score = (cu - MIN_CONSEC_UP) / (5 - MIN_CONSEC_UP) * 60
    cons_score = _clamp(cons_score, 0, 60)

    # 距 MA10 位置: 最佳在 1%~3%（刚突破），太近0分，太远0分
    dist = c["dist_from_ma10"] * 100
    if 1.0 <= dist <= 3.0:
        pos_score = 40
    elif 0 < dist < 1.0:
        pos_score = dist / 1.0 * 30
    elif 3.0 < dist <= 6.0:
        pos_score = (6.0 - dist) / 3.0 * 30
    else:
        pos_score = 0

    return _clamp(cons_score + pos_score, 0, 100)


def score_candidates(candidates: list[dict]) -> list[dict]:
    """对候选股进行4维度打分排序。"""
    for c in candidates:
        c["score_price"] = round(_score_price(c), 1)
        c["score_volume"] = round(_score_volume(c), 1)
        c["score_coordination"] = round(_score_coordination(c), 1)
        c["score_quality"] = round(_score_quality(c), 1)

        c["score"] = round(
            c["score_price"] * WEIGHT_PRICE
            + c["score_volume"] * WEIGHT_VOLUME
            + c["score_coordination"] * WEIGHT_COORD
            + c["score_quality"] * WEIGHT_QUALITY,
            1,
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    for i, c in enumerate(candidates, 1):
        c["rank"] = i
    return candidates


# ============ Phase 3: 报告输出 ============

def format_report(candidates: list[dict], market: dict, latest_date: str, top_n: int) -> str:
    """生成格式化报告。"""
    lines = []
    sep = "=" * 60
    sep2 = "-" * 60

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
    label = signal_label.get(market.get("signal", "YELLOW"), "[YELLOW]")

    lines.append(sep)
    lines.append(f"  价增量增策略 — 候选股扫描")
    lines.append(f"  数据日期: {_weekday_str(latest_date)} | 生成: {now_str}")
    lines.append(sep)

    # 大盘概况
    gate = market_gate(latest_date, market)
    lines.append("")
    lines.append(f"  大盘环境: {label} (Gate: {'允许' if gate['allowed'] else '禁止'})")
    if not gate["allowed"]:
        lines.append(f"  禁止原因: {'; '.join(gate['reasons'])}")
        lines.append("  [!] 大盘环境不佳，以下候选仅供参考，不建议买入")
    lines.append("")

    # 候选股列表
    if not candidates:
        lines.append("  (无符合条件的候选股)")
        lines.append("")
        lines.append(f"  策略: 价增量增 | 5日涨>{PRICE_UP_5D_MIN*100}% | 量比>{VOL_RATIO_BUY}x | 连续上涨≥{MIN_CONSEC_UP}天")
        return "\n".join(lines)

    show_n = min(len(candidates), top_n)
    lines.append(f"  发现 {len(candidates)} 只候选 | 展示前 {show_n} 只")
    lines.append("")
    lines.append(f"  {'排名':<4} {'代码':<12} {'名称':<8} {'评分':<6} {'5日涨':<8} {'量比':<7} {'连涨':<5} {'涨跌量比':<8} {'距MA10':<8}")
    lines.append(f"  {'-' * 70}")

    for c in candidates[:show_n]:
        lines.append(
            f"  {c['rank']:<4} "
            f"{c['code']:<12} "
            f"{c['name']:<8} "
            f"{c['score']:<6.0f} "
            f"{_fmt_pct(c['chg_5d']):<8} "
            f"{c['vol_ratio']:.1f}x{'':<4} "
            f"{c['consecutive_up']}天{'':<2} "
            f"{c['up_vol_ratio']:.1f}x{'':<4} "
            f"{c['dist_from_ma10']*100:+.1f}%"
        )

    # 详细分析（前3只）
    lines.append("")
    lines.append("  重点候选详细分析")
    lines.append(sep2)

    detail_n = min(len(candidates), max(top_n, 3))
    for c in candidates[:detail_n]:
        lines.append("")
        lines.append(f"  [{c['rank']}] {c['code']} {c['name']} — 综合评分 {c['score']:.0f}/100")
        lines.append(f"      价格: 5日{_fmt_pct(c['chg_5d'])} | 10日{_fmt_pct(c['chg_10d'])} | 20日{_fmt_pct(c['chg_20d'])}")
        lines.append(f"      量能: 量比{c['vol_ratio']:.1f}x(5日均量{c['vol_ma5']:.0f}/20日均量{c['vol_ma20']:.0f}) | 量加速{c['vol_trend_ratio']:.1f}x")
        lines.append(f"      量价: 涨跌量比{c['up_vol_ratio']:.1f}x | {c['up_days']}阳{c['down_days']}阴 | 连涨{c['consecutive_up']}天")
        lines.append(f"      位置: 距MA10({c['ma10']:.2f}) {c['dist_from_ma10']*100:+.1f}% | 今日{_fmt_pct(c['daily_chg'])}")
        lines.append(f"      分项: 价格{c['score_price']:.0f} | 量能{c['score_volume']:.0f} | 配合{c['score_coordination']:.0f} | 质量{c['score_quality']:.0f}")

    # 策略参数
    lines.append("")
    lines.append(f"  策略参数: 5日涨>{PRICE_UP_5D_MIN*100}% | 量比>{VOL_RATIO_BUY}x | 连续上涨≥{MIN_CONSEC_UP}天 | 涨跌量比>{UP_VOL_RATIO}x")

    # 风险提示
    lines.append("")
    lines.append(sep)
    lines.append("  免责声明: 基于历史数据的量化分析，不构成投资建议。")
    lines.append("  投资有风险，入市需谨慎。")
    lines.append(sep)

    return "\n".join(lines)


def format_qq_message(candidates: list[dict], market: dict, latest_date: str, top_n: int) -> str:
    """生成QQ推送用的简短消息。"""
    gate = market_gate(latest_date, market)
    gate_ok = gate["allowed"]
    gate_label = "可买" if gate_ok else "观望"

    lines = [
        f"[*] 价增量增 — {_weekday_str(latest_date)}",
        f"大盘: {gate_label} | 候选: {len(candidates)}只",
        "",
    ]

    if not candidates:
        lines.append("无符合条件的候选股")
        lines.append(f"条件: 5日涨>{PRICE_UP_5D_MIN*100}% | 量比>{VOL_RATIO_BUY}x | 连涨≥{MIN_CONSEC_UP}天")
    else:
        show_n = min(len(candidates), top_n)
        for c in candidates[:show_n]:
            lines.append(
                f"{c['rank']}. {c['name']}({c['code']}) "
                f"评分{c['score']:.0f} "
                f"| 5日涨{_fmt_pct(c['chg_5d'])} "
                f"| 量比{c['vol_ratio']:.1f}x "
                f"| 连涨{c['consecutive_up']}天 "
                f"| 涨跌量比{c['up_vol_ratio']:.1f}x"
            )

        if not gate_ok:
            lines.append("")
            lines.append(f"[!] 大盘不佳({'；'.join(gate['reasons'])})，仅供参考不推荐买入")

    lines.append("")
    lines.append("--- 价增量增策略 · 仅供参考 ---")

    return "\n".join(lines)


# ============ 主流程 ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="价增量增策略 — 每日扫描 + QQ推送")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="展示前N只候选")
    parser.add_argument("--push", action="store_true", help="推送到QQ")
    parser.add_argument("--json", action="store_true", help="输出JSON")
    parser.add_argument("--days", type=int, default=200, help="加载K线天数")
    args = parser.parse_args()

    print("=" * 60)
    print("  价增量增策略 — 候选股扫描")
    print("=" * 60)

    # ---- 1. 数据库连接 ----
    print("\n[1/4] 连接数据库...")
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        # ---- 2. 加载数据 ----
        print("[2/4] 加载数据...")
        latest = sess.query(func.max(StockDaily.trade_date)).scalar()
        cnt = sess.query(func.count(StockDaily.id)).filter(StockDaily.trade_date == latest).scalar()
        print(f"  数据库最新日期: {latest}, 当日数据: {cnt} 条")

        # 活跃股票列表
        latest_rows = (
            sess.query(StockInfo, StockDaily)
            .join(StockDaily, StockInfo.code == StockDaily.code)
            .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date == latest)
            .all()
        )
        stock_map = {
            stock.code: {"code": stock.code, "name": stock.name, "market": stock.market}
            for stock, _ in latest_rows
        }
        print(f"  活跃股票: {len(stock_map)} 只")

        # 最近N天K线
        date_rows = (
            sess.query(StockDaily.trade_date)
            .distinct()
            .order_by(desc(StockDaily.trade_date))
            .limit(args.days)
            .all()
        )
        if not date_rows:
            print("  错误: 数据库无K线数据，请先运行 update_daily.py")
            return
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
        print(f"  加载 {len(bars_by_code)} 只股票的K线数据 (最近{args.days}天)")

        # ---- 3. 大盘评估 + 候选扫描 + 评分 ----
        print("[3/4] 扫描候选股...")
        market_stats = _build_market_stats(bars_by_code)
        candidates = screen_candidates(bars_by_code, stock_map, latest)
        ranked = score_candidates(candidates)

        if ranked:
            top_score = ranked[0]["score"]
            print(f"  最高评分: {top_score:.0f} | 候选: {len(ranked)} 只")

        # ---- 4. 输出 ----
        print("[4/4] 生成报告...")
        report = format_report(ranked, market_stats, latest, args.top)
        print("\n" + report)

        # ---- JSON ----
        if args.json:
            json_path = ROOT_DIR / "data" / f"price_volume_rising_{latest}.json"
            json_data = {
                "date": latest,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "strategy": "price_volume_rising",
                "candidates": [
                    {
                        "rank": c["rank"],
                        "code": c["code"],
                        "name": c["name"],
                        "score": c["score"],
                        "chg_5d": c["chg_5d"],
                        "chg_10d": c["chg_10d"],
                        "vol_ratio": c["vol_ratio"],
                        "consecutive_up": c["consecutive_up"],
                        "up_vol_ratio": c["up_vol_ratio"],
                        "dist_from_ma10": c["dist_from_ma10"],
                        "score_breakdown": {
                            "price": c["score_price"],
                            "volume": c["score_volume"],
                            "coordination": c["score_coordination"],
                            "quality": c["score_quality"],
                        },
                    }
                    for c in ranked
                ],
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(json_data, f, ensure_ascii=False, indent=2)
            print(f"JSON已保存至: {json_path}")

        # ---- QQ推送 ----
        if args.push:
            print("\n[推送] 发送到QQ...")
            try:
                from models.qq_webhook import QQPusher, push_long_message
                pusher = QQPusher()
                if pusher.enabled:
                    qq_msg = format_qq_message(ranked, market_stats, latest, args.top)
                    result = push_long_message(qq_msg)
                    print(f"  QQ推送完成: success={result['success']}, fail={result['fail']}")
                else:
                    print("  QQ推送未启用，请检查 data/qq_config.json")
            except ImportError as e:
                print(f"  无法导入QQ推送模块: {e}")
            except Exception as e:
                print(f"  QQ推送失败: {e}")

    finally:
        sess.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
