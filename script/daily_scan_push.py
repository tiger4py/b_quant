#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日趋势跟随扫描 + QQ推送

核心理念：全市场无状态扫描，找出今天满足趋势跟随策略买入条件的股票，
按多维度评分排序，推送到QQ。

用法:
  python script/daily_scan_push.py              # 扫描 + 推送
  python script/daily_scan_push.py --top 5      # 推送前5只
  python script/daily_scan_push.py --no-push    # 仅打印，不推送
"""
import sys
import os
import time
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.indicators import sma

# 趋势跟随策略参数（与 strategy_trend_following.py 同步）
from backtest.strategy.strategy_trend_following import (
    PRICE_UP_5D_MIN, PRICE_UP_20D_MIN, PRICE_DOWN_40D_MAX,
    PRICE_NEAR_20D_HIGH, PRICE_ABOVE_MA20_MAX,
    VOL_RATIO_BUY, VOL_RATIO_MAX, VOL_TREND_ACCEL,
    UP_VOL_RATIO, LOOKBACK_DAYS, MIN_CONSEC_UP,
    _compute_price_volume_dynamics,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ============ 无状态买入检测 ============

def check_buy_today(bars):
    """
    无状态检测：最新一根K线是否满足趋势跟随策略的所有买入条件。
    不做持仓跟踪，纯粹判断「今天这个点位该不该买」。

    返回: dict（含信号详情+评分）或 None
    """
    if len(bars) < 45:
        return None

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    idx = len(closes) - 1
    close = closes[idx]

    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    if ma10[idx] is None or ma20[idx] is None:
        return None
    if vol_ma5[idx] is None or vol_ma20[idx] is None or vol_ma20[idx] == 0:
        return None

    vol_ratio = vol_ma5[idx] / vol_ma20[idx]

    # ---- 趋势过滤 ----
    if closes[idx - 5] <= 0:
        return None
    chg_5d = (close - closes[idx - 5]) / closes[idx - 5]
    if chg_5d < PRICE_UP_5D_MIN:
        return None

    chg_20d = (close - closes[idx - 20]) / closes[idx - 20] if closes[idx - 20] > 0 else 0
    if chg_20d < PRICE_UP_20D_MIN:
        return None

    # 40日最大回撤（滚动峰值法）
    peak_40 = closes[idx - 39]
    max_dd_40 = 0.0
    for j in range(idx - 38, idx + 1):
        if closes[j] > peak_40:
            peak_40 = closes[j]
        dd = (closes[j] - peak_40) / peak_40
        if dd < max_dd_40:
            max_dd_40 = dd
    if max_dd_40 < PRICE_DOWN_40D_MAX:
        return None

    # ---- 位置过滤 ----
    if close <= ma10[idx] or close <= ma20[idx]:
        return None

    high_20 = max(highs[idx - 19:idx + 1])
    if (high_20 - close) / high_20 > PRICE_NEAR_20D_HIGH:
        return None

    if close > ma20[idx] * PRICE_ABOVE_MA20_MAX:
        return None

    # ---- 量能过滤 ----
    if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
        return None

    # 量能加速
    if idx >= 6:
        recent_3 = sum(volumes[idx - 2:idx + 1]) / 3
        prior_3 = sum(volumes[idx - 5:idx - 2]) / 3
        if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
            return None

    # ---- 量价配合 ----
    dyn = _compute_price_volume_dynamics(closes, volumes, idx)
    if dyn["up_vol_ratio"] < UP_VOL_RATIO:
        return None
    if dyn["consecutive_up"] < MIN_CONSEC_UP:
        return None

    # ---- 评分 ----
    chg_10d = (close - closes[idx - 10]) / closes[idx - 10] if idx >= 10 and closes[idx - 10] > 0 else 0
    dist_ma20 = (close - ma20[idx]) / ma20[idx]

    score_trend = min(30, max(0, chg_5d * 100 * 2 + (chg_20d > 0) * 10))
    score_vol = min(25, max(0, (vol_ratio - 1.3) / 2.7 * 25))
    score_coord = min(25, max(0, (dyn["up_vol_ratio"] - 1.1) / 1.4 * 25))
    score_pos = 20
    if dist_ma20 < 0.01: score_pos -= 5
    elif dist_ma20 > 0.10: score_pos -= 10
    if dyn["consecutive_up"] < 2: score_pos -= 5
    total_score = score_trend + score_vol + score_coord + score_pos

    return {
        "close": close,
        "ma20": round(ma20[idx], 2),
        "chg_5d": round(chg_5d * 100, 1),
        "chg_10d": round(chg_10d * 100, 1),
        "chg_20d": round(chg_20d * 100, 1),
        "vol_ratio": round(vol_ratio, 1),
        "up_vol_ratio": round(dyn["up_vol_ratio"], 1),
        "consecutive_up": dyn["consecutive_up"],
        "dist_ma20": round(dist_ma20 * 100, 1),
        "daily_chg": round((closes[idx] - closes[idx - 1]) / closes[idx - 1] * 100, 1) if idx > 0 else 0,
        "score": round(total_score, 1),
        "scores": {
            "trend": round(score_trend, 1),
            "volume": round(score_vol, 1),
            "coord": round(score_coord, 1),
            "position": round(score_pos, 1),
        },
    }


# ============ 全市场扫描 ============

def scan_all(sess, latest_date, min_amount=50_000_000):
    """全市场扫描，返回按评分降序的候选列表。"""
    print(f"数据日期: {latest_date}")
    print(f"全市场扫描中...")

    # 加载活跃股票列表（最新交易日成交额 > min_amount）
    active = (
        sess.query(StockInfo.code, StockInfo.name)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date == latest_date,
            StockDaily.amount >= min_amount,
        )
        .all()
    )
    active_codes = {r.code for r in active}
    name_map = {r.code: r.name for r in active}
    print(f"  活跃股票: {len(active_codes)} 只（成交额≥{min_amount/1e6:.0f}M）")

    # 加载K线（最近200天）
    date_rows = (
        sess.query(StockDaily.trade_date).distinct()
        .order_by(desc(StockDaily.trade_date)).limit(200).all()
    )
    if not date_rows:
        print("  无K线数据")
        return []
    cutoff = min(r[0] for r in date_rows)

    rows = (
        sess.query(StockDaily)
        .join(StockInfo, StockDaily.code == StockInfo.code)
        .filter(
            StockInfo.type == "1",
            StockInfo.status == 1,
            StockDaily.trade_date >= cutoff,
        )
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )

    bars_by_code = defaultdict(list)
    for r in rows:
        if r.code in active_codes:
            bars_by_code[r.code].append({
                "trade_date": r.trade_date,
                "open": r.open, "high": r.high, "low": r.low,
                "close": r.close, "volume": r.volume, "amount": r.amount,
            })

    print(f"  加载K线: {len(bars_by_code)} 只股票")

    # 扫描
    candidates = []
    t0 = time.time()
    for code, bars in bars_by_code.items():
        if len(bars) < 45:
            continue
        if bars[-1]["trade_date"] != latest_date:
            continue

        result = check_buy_today(bars)
        if result is None:
            continue

        result["code"] = code
        result["name"] = name_map.get(code, code)
        candidates.append(result)

    elapsed = time.time() - t0
    candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"  扫描完成: {len(candidates)} 只候选，耗时 {elapsed:.0f}s")
    return candidates


# ============ 格式化输出 ============

def _weekday(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{date_str} ({['周一','周二','周三','周四','周五','周六','周日'][dt.weekday()]})"
    except Exception:
        return date_str


def print_candidates(candidates, top_n=5):
    """控制台打印候选股。"""
    print(f"\n{'=' * 60}")
    print(f"  趋势跟随 — 今日买入候选 (前{top_n}只)")
    print(f"{'=' * 60}")

    if not candidates:
        print("  无符合条件的股票")
        return

    for i, c in enumerate(candidates[:top_n], 1):
        print(f"\n  [{i}] {c['code']} {c['name']} — 综合评分 {c['score']:.0f}")
        print(f"      现价 {c['close']:.2f} | MA20 {c['ma20']:.2f} | 距MA20 {c['dist_ma20']:+.1f}%")
        print(f"      5日 {c['chg_5d']:+.1f}% | 10日 {c['chg_10d']:+.1f}% | 20日 {c['chg_20d']:+.1f}% | 今日 {c['daily_chg']:+.1f}%")
        print(f"      量比 {c['vol_ratio']:.1f}x | 涨跌量比 {c['up_vol_ratio']:.1f}x | 连涨 {c['consecutive_up']}天")
        print(f"      分项: 趋势{c['scores']['trend']:.0f} 量能{c['scores']['volume']:.0f} 配合{c['scores']['coord']:.0f} 位置{c['scores']['position']:.0f}")


def build_qq_message(candidates, latest_date, top_n=5):
    """构建QQ推送消息。"""
    wd = _weekday(latest_date)
    lines = [
        f"[*] 趋势跟随 — {wd}",
        f"全市场扫描: {len(candidates)} 只候选",
        "",
    ]

    if not candidates:
        lines.append("今日无符合条件的买入信号")
    else:
        for i, c in enumerate(candidates[:top_n], 1):
            lines.append(
                f"{i}. {c['name']}({c['code']}) 评分{c['score']:.0f}"
            )
            lines.append(
                f"   {c['close']:.2f}元 | 5日{c['chg_5d']:+.1f}% 20日{c['chg_20d']:+.1f}% | "
                f"量比{c['vol_ratio']:.1f}x 涨跌量比{c['up_vol_ratio']:.1f}x 连涨{c['consecutive_up']}天"
            )
            lines.append(
                f"   趋势{c['scores']['trend']:.0f} 量能{c['scores']['volume']:.0f} "
                f"配合{c['scores']['coord']:.0f} 位置{c['scores']['position']:.0f}"
            )

    lines.append("")
    lines.append("--- 趋势跟随 · 仅供参考 ---")
    return "\n".join(lines)


def push_qq(msg):
    """推送到QQ。"""
    try:
        from models.qq_webhook import QQPusher
        pusher = QQPusher()
        if pusher.enabled:
            result = pusher.push_long_text(msg)
            print(f"\n[QQ] 推送完成: success={result['success']}, fail={result['fail']}")
        else:
            print("\n[QQ] 推送未启用，检查 data/qq_config.json")
    except Exception as e:
        print(f"\n[QQ] 推送异常: {e}")


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser(description="趋势跟随每日扫描 + QQ推送")
    parser.add_argument("--top", type=int, default=5, help="推送前N只")
    parser.add_argument("--min-amount", type=float, default=50_000_000, help="最低成交额过滤（默认5000万）")
    parser.add_argument("--no-push", action="store_true", help="不推送QQ，仅打印")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
        if not latest_date:
            print("[!] 数据库无K线数据，请先运行 update_daily.py")
            return

        # 全市场扫描
        candidates = scan_all(sess, latest_date, min_amount=args.min_amount)

        # 控制台输出
        print_candidates(candidates, top_n=args.top)

        # QQ推送
        if not args.no_push:
            msg = build_qq_message(candidates, latest_date, top_n=args.top)
            print(f"\n[推送内容预览]")
            print(msg)
            push_qq(msg)

    finally:
        sess.close()


if __name__ == "__main__":
    main()
