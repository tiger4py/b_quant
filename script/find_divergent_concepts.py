# -*- coding: utf-8 -*-
"""
找出最近30天内"股性"完全不一样的概念板块。

对每个概念，计算两个时间窗口的"股性指纹"（多维特征向量），
然后按窗口间距离排序，找出行为变化最大的概念。

股性维度:
  1. 日均收益率 (return_mean)
  2. 日波动率 (return_std)
  3. 收益率偏度 (return_skew) — 暴涨 vs 暴跌
  4. 上涨天数占比 (up_day_ratio)
  5. 日均成交额 (amount_mean, log)
  6. 成交额变异系数 (amount_cv)
  7. 趋势斜率 (trend_slope) — 线性回归
  8. 趋势 R² (trend_r2) — 趋势稳定性
  9. 最大回撤 (max_drawdown)
 10. 1日自相关 (autocorr_1) — 趋势跟随 vs 均值回归
 11. 5日累计收益 (ret_5d) — 短期动量
 12. 振幅均值 (amplitude_mean) — 日内波动

用法:
    python script/find_divergent_concepts.py
    python script/find_divergent_concepts.py --top 20
    python script/find_divergent_concepts.py --recent-days 30 --history-days 120
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# Fix Windows GBK encoding issue
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

# ======== 数据加载 ========


def load_concept_bars_from_files():
    """从 data/concept/ CSV 文件加载所有概念日线数据。"""
    import csv
    import glob as _glob

    csv_dir = str(ROOT_DIR / "data" / "concept")
    pattern = os.path.join(csv_dir, "*", "*.csv")
    csv_files = _glob.glob(pattern)

    grouped = defaultdict(list)
    name_map = {}

    for fp in csv_files:
        basename = os.path.basename(fp)
        if not basename[:4].isdigit():
            continue
        with open(fp, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row.get("concept_code", "")
                name = row.get("concept_name", "")
                if name:
                    name_map[code] = name
                vol_raw = row.get("volume")
                amt_raw = row.get("amount")
                grouped[code].append({
                    "trade_date": row.get("trade_date", ""),
                    "open": float(row["open"]) if row.get("open") else None,
                    "high": float(row["high"]) if row.get("high") else None,
                    "low": float(row["low"]) if row.get("low") else None,
                    "close": float(row["close"]) if row.get("close") else None,
                    "volume": int(float(vol_raw)) if vol_raw and vol_raw != "None" else 0,
                    "amount": float(amt_raw) if amt_raw and amt_raw != "None" else 0,
                })

    # 排序 & 过滤掉数据太少的
    bars_by_code = {}
    for code, bars in grouped.items():
        bars.sort(key=lambda b: b["trade_date"])
        if len(bars) >= 60:  # 至少需要60个交易日
            bars_by_code[code] = bars

    return bars_by_code, name_map


# ======== 股性特征计算 ========


def compute_features(bars, window_dates=None):
    """给定一组日线数据，计算股性特征向量。

    如果指定 window_dates (set of date strings)，只使用该窗口内的数据。
    返回 dict: {feature_name: value}
    """
    if window_dates is not None:
        bars = [b for b in bars if b["trade_date"] in window_dates]
    if len(bars) < 10:
        return None

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    amounts = [b["amount"] for b in bars if b["amount"] and b["amount"] > 0]

    n = len(closes)

    # 日收益率
    returns = []
    for i in range(1, n):
        if closes[i - 1] and closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])

    if len(returns) < 5:
        return None

    m = len(returns)

    # 1. 日均收益率
    return_mean = sum(returns) / m

    # 2. 日波动率 (收益率标准差)
    return_var = sum((r - return_mean) ** 2 for r in returns) / m
    return_std = math.sqrt(return_var)

    # 3. 收益率偏度
    if return_std > 0:
        return_skew = sum((r - return_mean) ** 3 for r in returns) / m / (return_std ** 3)
    else:
        return_skew = 0.0

    # 4. 上涨天数占比
    up_days = sum(1 for r in returns if r > 0)
    up_day_ratio = up_days / m

    # 5. 日均成交额 (log)
    if amounts:
        amount_mean = sum(amounts) / len(amounts)
        amount_mean_log = math.log(max(amount_mean, 1))
    else:
        amount_mean_log = 0.0

    # 6. 成交额变异系数
    if amounts and amount_mean > 0:
        amount_std = math.sqrt(sum((a - sum(amounts) / len(amounts)) ** 2 for a in amounts) / len(amounts))
        amount_cv = amount_std / (sum(amounts) / len(amounts))
    else:
        amount_cv = 0.0

    # 7-8. 趋势斜率 & R² (线性回归 close ~ day_index)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(closes) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, closes))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_yy = sum((y - mean_y) ** 2 for y in closes)
    if ss_xx > 0 and ss_yy > 0:
        trend_slope = ss_xy / ss_xx
        # 归一化斜率: 除以首日收盘价得到百分比趋势
        if closes[0] > 0:
            trend_slope_pct = trend_slope / closes[0] * 100  # 每交易日%
        else:
            trend_slope_pct = 0.0
        trend_r2 = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx * ss_yy > 0 else 0.0
    else:
        trend_slope_pct = 0.0
        trend_r2 = 0.0

    # 9. 最大回撤
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # 10. 1日自相关
    if m >= 3 and return_std > 0:
        mean_r = sum(returns) / m
        cov = sum((returns[i] - mean_r) * (returns[i - 1] - mean_r) for i in range(1, m)) / (m - 1)
        autocorr_1 = cov / (return_var) if return_var > 0 else 0.0
    else:
        autocorr_1 = 0.0

    # 11. 5日累计收益
    if n >= 5 and closes[-5] and closes[-5] > 0:
        ret_5d = (closes[-1] - closes[-5]) / closes[-5]
    elif n >= 2 and closes[0] > 0:
        ret_5d = (closes[-1] - closes[0]) / closes[0]
    else:
        ret_5d = 0.0

    # 12. 日内振幅均值
    amplitudes = []
    for i in range(n):
        if highs[i] and lows[i] and closes[i - 1 if i > 0 else 0] and closes[i - 1 if i > 0 else 0] > 0:
            amp = (highs[i] - lows[i]) / closes[i - 1] if i > 0 else (highs[i] - lows[i]) / closes[i]
            if 0 < amp < 0.5:  # 过滤异常值
                amplitudes.append(amp)
    amplitude_mean = sum(amplitudes) / len(amplitudes) if amplitudes else 0.0

    return {
        "return_mean": return_mean,
        "return_std": return_std,
        "return_skew": return_skew,
        "up_day_ratio": up_day_ratio,
        "amount_mean_log": amount_mean_log,
        "amount_cv": amount_cv,
        "trend_slope_pct": trend_slope_pct,
        "trend_r2": trend_r2,
        "max_drawdown": max_dd,
        "autocorr_1": autocorr_1,
        "ret_5d": ret_5d,
        "amplitude_mean": amplitude_mean,
        "n_days": n,
    }


# ======== 距离计算 ========


# 每个特征的归一化权重（用于计算加权欧氏距离）
# 权重反映该维度在判断"股性变化"时的重要性
FEATURE_WEIGHTS = {
    "return_mean": 1.5,       # 收益率方向变化很重要
    "return_std": 2.0,         # 波动率变化非常重要
    "return_skew": 1.0,        # 偏度变化
    "up_day_ratio": 1.0,       # 涨跌比
    "amount_mean_log": 1.5,    # 量能级别变化
    "amount_cv": 0.8,          # 量能稳定性
    "trend_slope_pct": 2.0,    # 趋势方向变化非常重要
    "trend_r2": 1.5,           # 趋势稳定性变化
    "max_drawdown": 1.5,       # 回撤特征变化
    "autocorr_1": 1.5,         # 自相关性变化（趋势→震荡）
    "ret_5d": 1.0,             # 短期收益变化
    "amplitude_mean": 1.0,     # 振幅变化
}

FEATURE_NAMES_CN = {
    "return_mean": "日均收益",
    "return_std": "波动率",
    "return_skew": "收益偏度",
    "up_day_ratio": "上涨占比",
    "amount_mean_log": "成交额(log)",
    "amount_cv": "量能变异",
    "trend_slope_pct": "趋势斜率",
    "trend_r2": "趋势R²",
    "max_drawdown": "最大回撤",
    "autocorr_1": "自相关(1日)",
    "ret_5d": "5日收益",
    "amplitude_mean": "日内振幅",
}


def normalize_features(all_features):
    """对每个特征做 z-score 归一化（跨所有概念），使得不同维度的距离可比。"""
    feature_keys = list(FEATURE_WEIGHTS.keys())
    n = len(all_features)

    means = {k: 0.0 for k in feature_keys}
    stds = {k: 0.0 for k in feature_keys}

    for ft in all_features:
        for k in feature_keys:
            means[k] += ft.get(k, 0.0) / n

    for ft in all_features:
        for k in feature_keys:
            stds[k] += (ft.get(k, 0.0) - means[k]) ** 2 / n

    for k in feature_keys:
        stds[k] = math.sqrt(max(stds[k], 1e-10))

    normalized = []
    for ft in all_features:
        norm = {}
        for k in feature_keys:
            raw = ft.get(k, 0.0)
            if stds[k] > 1e-10:
                norm[k] = (raw - means[k]) / stds[k]
            else:
                norm[k] = 0.0
        normalized.append(norm)

    return normalized, means, stds


def weighted_euclidean_distance(f1, f2, weights=None):
    """计算两个特征向量之间的加权欧氏距离。"""
    if weights is None:
        weights = FEATURE_WEIGHTS
    total = 0.0
    for k, w in weights.items():
        v1 = f1.get(k, 0.0)
        v2 = f2.get(k, 0.0)
        total += w * (v1 - v2) ** 2
    return math.sqrt(total)


def cosine_distance(f1, f2, weights=None):
    """计算加权余弦距离。"""
    if weights is None:
        weights = FEATURE_WEIGHTS
    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for k, w in weights.items():
        v1 = f1.get(k, 0.0)
        v2 = f2.get(k, 0.0)
        dot += w * v1 * v2
        norm1 += w * v1 * v1
        norm2 += w * v2 * v2
    if norm1 > 0 and norm2 > 0:
        cos_sim = dot / (math.sqrt(norm1) * math.sqrt(norm2))
        # clamp
        cos_sim = max(-1.0, min(1.0, cos_sim))
        return 1.0 - cos_sim  # distance = 1 - similarity
    return 1.0


# ======== 特征变化方向描述 ========


def describe_change(ft_recent, ft_history, feature_key):
    """描述单个特征的变化方向。"""
    v1 = ft_recent.get(feature_key, 0)
    v2 = ft_history.get(feature_key, 0)
    diff = v1 - v2
    name = FEATURE_NAMES_CN.get(feature_key, feature_key)

    if abs(diff) < 0.3:  # 归一化后小差异忽略
        return None

    direction = "↑升高" if diff > 0 else "↓下降"
    magnitude = ""
    if abs(diff) > 1.5:
        magnitude = "【大幅】"
    elif abs(diff) > 0.8:
        magnitude = "【显著】"

    return f"{name}{magnitude}{direction}"


# ======== 主分析 ========


@dataclass
class DivergentConcept:
    code: str
    name: str
    distance_euc: float
    distance_cos: float
    ranked_score: float  # 综合排名分
    changes: list  # 显著变化的维度描述
    ft_recent: dict
    ft_history: dict
    n_recent_days: int
    n_history_days: int


def find_divergent_concepts(
    recent_days=30,
    history_days=120,
    gap_days=0,
    top_n=30,
):
    """主分析函数。

    参数:
        recent_days: 近期窗口交易日数
        history_days: 历史对比窗口交易日数
        gap_days: 两个窗口之间的间隔交易日数
        top_n: 返回前N个变化最大的概念
    """
    print(f"加载概念日线数据...")
    bars_by_code, name_map = load_concept_bars_from_files()
    print(f"共 {len(bars_by_code)} 个概念有足够数据")

    # 确定所有可用的交易日期
    all_dates = set()
    for bars in bars_by_code.values():
        for b in bars:
            all_dates.add(b["trade_date"])
    all_dates = sorted(all_dates)
    print(f"日期范围: {all_dates[0]} ~ {all_dates[-1]}, 共 {len(all_dates)} 个交易日")

    # 最近的交易日
    latest_date = all_dates[-1]
    print(f"最新交易日: {latest_date}")

    # 构建近期窗口和历史窗口的日期集合
    recent_end_idx = len(all_dates) - 1
    recent_start_idx = max(0, recent_end_idx - recent_days)
    recent_dates = set(all_dates[recent_start_idx:recent_end_idx + 1])

    history_end_idx = recent_start_idx - gap_days
    history_start_idx = max(0, history_end_idx - history_days)
    history_dates = set(all_dates[history_start_idx:history_end_idx + 1])

    print(f"近期窗口: {all_dates[recent_start_idx]} ~ {all_dates[recent_end_idx]} ({len(recent_dates)} 天)")
    print(f"历史窗口: {all_dates[history_start_idx]} ~ {all_dates[history_end_idx]} ({len(history_dates)} 天)")
    print()

    # 对每个概念计算两个窗口的特征
    all_recent_features = []
    all_history_features = []
    valid_codes = []

    for code, bars in bars_by_code.items():
        ft_r = compute_features(bars, recent_dates)
        ft_h = compute_features(bars, history_dates)
        if ft_r is not None and ft_h is not None:
            all_recent_features.append(ft_r)
            all_history_features.append(ft_h)
            valid_codes.append(code)

    print(f"有效概念: {len(valid_codes)} 个")

    # 合并所有特征做全局归一化
    all_features = all_recent_features + all_history_features
    normalized_all, means, stds = normalize_features(all_features)
    norm_recent = normalized_all[:len(valid_codes)]
    norm_history = normalized_all[len(valid_codes):]

    # 计算每个概念的距离
    results = []
    for i, code in enumerate(valid_codes):
        ft_r_norm = norm_recent[i]
        ft_h_norm = norm_history[i]
        ft_r_raw = all_recent_features[i]
        ft_h_raw = all_history_features[i]

        dist_euc = weighted_euclidean_distance(ft_r_norm, ft_h_norm)
        dist_cos = cosine_distance(ft_r_norm, ft_h_norm)

        # 找出变化最大的特征维度
        changes = []
        for k in FEATURE_WEIGHTS:
            diff = abs(ft_r_norm.get(k, 0) - ft_h_norm.get(k, 0))
            if diff > 0.5:  # 超过0.5个标准差才算显著
                desc = describe_change(ft_r_norm, ft_h_norm, k)
                if desc:
                    changes.append((diff, desc))

        changes.sort(key=lambda x: -x[0])
        change_descs = [c[1] for c in changes[:6]]  # 最多6个主要变化

        # 综合排名分: EUC 和 COS 各自归一化后平均
        results.append(DivergentConcept(
            code=code,
            name=name_map.get(code, code),
            distance_euc=dist_euc,
            distance_cos=dist_cos,
            ranked_score=0.0,  # 稍后计算
            changes=change_descs,
            ft_recent=ft_r_raw,
            ft_history=ft_h_raw,
            n_recent_days=ft_r_raw["n_days"],
            n_history_days=ft_h_raw["n_days"],
        ))

    # 归一化两种距离后计算综合分数
    euc_values = [r.distance_euc for r in results]
    cos_values = [r.distance_cos for r in results]
    euc_mean = sum(euc_values) / len(euc_values)
    euc_std = math.sqrt(sum((v - euc_mean) ** 2 for v in euc_values) / len(euc_values))
    cos_mean = sum(cos_values) / len(cos_values)
    cos_std = math.sqrt(sum((v - cos_mean) ** 2 for v in cos_values) / len(cos_values))

    for r in results:
        euc_z = (r.distance_euc - euc_mean) / max(euc_std, 1e-10)
        cos_z = (r.distance_cos - cos_mean) / max(cos_std, 1e-10)
        r.ranked_score = (euc_z + cos_z) / 2.0

    # 按综合分数排序
    results.sort(key=lambda r: -r.ranked_score)

    return results[:top_n], {
        "total_concepts": len(bars_by_code),
        "valid_concepts": len(valid_codes),
        "recent_window": f"{all_dates[recent_start_idx]} ~ {all_dates[recent_end_idx]}",
        "history_window": f"{all_dates[history_start_idx]} ~ {all_dates[history_end_idx]}",
        "latest_date": latest_date,
    }


# ======== 输出 ========


def print_results(results, meta, show_detail=True):
    """格式化打印分析结果。"""
    print()
    print("=" * 100)
    print(f"  概念板块「股性」变化分析 — 最近30天 vs 历史")
    print(f"  数据范围: {meta['valid_concepts']} 个概念 | "
          f"近期窗口: {meta['recent_window']} | 历史窗口: {meta['history_window']}")
    print("=" * 100)
    print()

    header = (
        f"{'排名':<5} {'概念名称':<20} {'代码':<8} "
        f"{'综合分':>7} {'EUC':>7} {'COS':>7} "
        f"{'近期':>5}d {'历史':>5}d | 主要变化"
    )
    print(header)
    print("-" * 100)

    for rank, r in enumerate(results, 1):
        changes_str = " | ".join(r.changes[:4]) if r.changes else "(无显著单维变化)"
        print(
            f"{rank:<5} {r.name:<20} {r.code:<8} "
            f"{r.ranked_score:>+7.2f} {r.distance_euc:>7.2f} {r.distance_cos:>7.2f} "
            f"{r.n_recent_days:>5} {r.n_history_days:>5} | {changes_str}"
        )

    if show_detail and results:
        # 详细展示 Top 5
        print()
        print("=" * 100)
        print("  Top 5 详细对比")
        print("=" * 100)
        for rank, r in enumerate(results[:5], 1):
            print()
            print(f"━━━ #{rank} {r.name} ({r.code}) — 综合分: {r.ranked_score:+.2f} ━━━")
            print(f"  近期({r.n_recent_days}d) vs 历史({r.n_history_days}d)")
            print()
            print(f"  {'维度':<18} {'近期值':>10} {'历史值':>10} {'变化':>10} {'变化%':>8}")
            print(f"  {'-'*56}")

            ft_r = r.ft_recent
            ft_h = r.ft_history

            metric_format = {
                "return_mean": ("{:.4f}", "{:+.2%}"),
                "return_std": ("{:.4f}", "{:+.2%}"),
                "return_skew": ("{:.3f}", "{:+.3f}"),
                "up_day_ratio": ("{:.3f}", "{:.1%}"),
                "amount_mean_log": ("{:.2f}", "{:.2f}"),
                "amount_cv": ("{:.3f}", "{:.3f}"),
                "trend_slope_pct": ("{:.4f}", "{:+.4f}%"),
                "trend_r2": ("{:.3f}", "{:.3f}"),
                "max_drawdown": ("{:.4f}", "{:.1%}"),
                "autocorr_1": ("{:.3f}", "{:+.3f}"),
                "ret_5d": ("{:.4f}", "{:+.2%}"),
                "amplitude_mean": ("{:.4f}", "{:.2%}"),
            }

            for k in FEATURE_WEIGHTS:
                v1 = ft_r.get(k, 0)
                v2 = ft_h.get(k, 0)
                diff = v1 - v2
                pct = (diff / abs(v2)) * 100 if abs(v2) > 1e-8 else (diff * 100 if abs(diff) > 1e-8 else 0)

                raw_fmt, display_fmt = metric_format.get(k, ("{:.4f}", "{:.4f}"))
                name = FEATURE_NAMES_CN.get(k, k)
                marker = " <<<" if abs(diff) / max(abs(v2), 1e-8) > 0.3 else ""

                print(f"  {name:<18} {display_fmt.format(v1):>10} {display_fmt.format(v2):>10} "
                      f"{display_fmt.format(diff):>10} {pct:>+7.1f}%{marker}")

            # 解读
            print()
            print(f"  📊 股性变化解读:")
            interpretations = interpret_changes(r)
            for interp in interpretations:
                print(f"     • {interp}")

    print()


def interpret_changes(r: DivergentConcept):
    """根据特征变化生成人类可读的解读。"""
    interps = []
    ft_r = r.ft_recent
    ft_h = r.ft_history

    # 趋势方向变化
    slope_r = ft_r["trend_slope_pct"]
    slope_h = ft_h["trend_slope_pct"]
    r2_r = ft_r["trend_r2"]
    r2_h = ft_h["trend_r2"]

    if slope_h > 0.01 and slope_r < -0.01:
        interps.append(f"趋势从上升转为下降 (斜率 {slope_h:+.3f}% → {slope_r:+.3f}%/天)")
    elif slope_h < -0.01 and slope_r > 0.01:
        interps.append(f"趋势从下降转为上升 (斜率 {slope_h:+.3f}% → {slope_r:+.3f}%/天)")

    if r2_r > 0.6 and r2_h < 0.3:
        interps.append(f"从震荡无序变为强趋势 (R² {r2_h:.2f} → {r2_r:.2f})")
    elif r2_h > 0.6 and r2_r < 0.3:
        interps.append(f"从强趋势变为震荡无序 (R² {r2_h:.2f} → {r2_r:.2f})")

    # 波动率变化
    vol_r = ft_r["return_std"]
    vol_h = ft_h["return_std"]
    if vol_h > 0 and vol_r / vol_h > 1.5:
        interps.append(f"波动率大幅升高 {vol_r/vol_h:.1f}x ({vol_h:.4f} → {vol_r:.4f})")
    elif vol_h > 0 and vol_r / vol_h < 0.5:
        interps.append(f"波动率大幅降低 {vol_r/vol_h:.1f}x ({vol_h:.4f} → {vol_r:.4f})")

    # 量能变化
    amt_r = ft_r["amount_mean_log"]
    amt_h = ft_h["amount_mean_log"]
    if amt_r - amt_h > 1.0:
        interps.append(f"成交额显著放大 ({math.exp(amt_h):.0f} → {math.exp(amt_r):.0f})")
    elif amt_h - amt_r > 1.0:
        interps.append(f"成交额显著萎缩 ({math.exp(amt_h):.0f} → {math.exp(amt_r):.0f})")

    # 自相关变化
    ac_r = ft_r["autocorr_1"]
    ac_h = ft_h["autocorr_1"]
    if ac_h > 0.3 and ac_r < -0.1:
        interps.append(f"从趋势跟随转为均值回归 (自相关 {ac_h:+.3f} → {ac_r:+.3f})")
    elif ac_h < -0.1 and ac_r > 0.3:
        interps.append(f"从均值回归转为趋势跟随 (自相关 {ac_h:+.3f} → {ac_r:+.3f})")

    # 偏度变化
    skew_r = ft_r["return_skew"]
    skew_h = ft_h["return_skew"]
    if skew_h > 0.5 and skew_r < -0.5:
        interps.append(f"从暴涨倾向转为暴跌倾向 (偏度 {skew_h:+.2f} → {skew_r:+.2f})")
    elif skew_h < -0.5 and skew_r > 0.5:
        interps.append(f"从暴跌倾向转为暴涨倾向 (偏度 {skew_h:+.2f} → {skew_r:+.2f})")

    # 日内振幅变化
    amp_r = ft_r["amplitude_mean"]
    amp_h = ft_h["amplitude_mean"]
    if amp_h > 0 and amp_r / amp_h > 1.5:
        interps.append(f"日内振幅显著变大 ({amp_h:.2%} → {amp_r:.2%})")
    elif amp_h > 0 and amp_r / amp_h < 0.5:
        interps.append(f"日内振幅显著变小 ({amp_h:.2%} → {amp_r:.2%})")

    if not interps:
        interps.append("多维度综合变化，未触发单一维度的极端阈值")

    return interps


def save_results(results, meta):
    """保存结果到 JSON 文件。"""
    output_dir = ROOT_DIR / "data" / "factor_lab"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"divergent_concepts_{stamp}.json"

    output = {
        "meta": meta,
        "results": [
            {
                "rank": i + 1,
                "code": r.code,
                "name": r.name,
                "ranked_score": round(r.ranked_score, 4),
                "distance_euc": round(r.distance_euc, 4),
                "distance_cos": round(r.distance_cos, 4),
                "changes": r.changes,
                "ft_recent": {k: round(v, 6) for k, v in r.ft_recent.items()},
                "ft_history": {k: round(v, 6) for k, v in r.ft_history.items()},
            }
            for i, r in enumerate(results, 1)
        ],
    }

    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已保存: {path}")
    return path


# ======== CLI ========


def main():
    parser = argparse.ArgumentParser(description="找出股性完全不一样的概念板块")
    parser.add_argument("--recent-days", type=int, default=30,
                        help="近期窗口交易日数 (默认 30)")
    parser.add_argument("--history-days", type=int, default=120,
                        help="历史对比窗口交易日数 (默认 120)")
    parser.add_argument("--gap-days", type=int, default=0,
                        help="两窗口间隔交易日数 (默认 0)")
    parser.add_argument("--top", type=int, default=30,
                        help="输出前N个 (默认 30)")
    parser.add_argument("--save", action="store_true", default=True,
                        help="保存JSON结果")
    parser.add_argument("--no-detail", action="store_true",
                        help="不显示Top 5详细对比")
    args = parser.parse_args()

    results, meta = find_divergent_concepts(
        recent_days=args.recent_days,
        history_days=args.history_days,
        gap_days=args.gap_days,
        top_n=args.top,
    )

    print_results(results, meta, show_detail=not args.no_detail)

    if args.save:
        save_results(results, meta)


if __name__ == "__main__":
    main()
