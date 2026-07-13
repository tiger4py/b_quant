#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日指导报告 — 基于波动率V反策略的次日交易决策

回答两个核心问题:
  1. 明天要不要买？ → 大盘环境 GREEN/YELLOW/RED + 操作建议
  2. 买啥？         → 按综合评分排序的候选股清单 + 详细分析

用法:
  python script/daily_guide.py              # 标准输出
  python script/daily_guide.py --top 5      # 只显示前5只
  python script/daily_guide.py --push        # 同时推送到QQ
  python script/daily_guide.py --json        # 输出JSON格式
"""
import sys
import json
import re
import os
from pathlib import Path
from datetime import datetime, timedelta

# 修复 Windows GBK 终端编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Emoji 兼容：Windows终端可能不支持emoji，使用纯文本替代
def _icon(name: str) -> str:
    """返回跨平台兼容的图标"""
    icons = {
        "green": "[GREEN]",
        "yellow": "[YELLOW]",
        "red": "[RED]",
        "warn": "[!]",
        "chart": "[*]",
    }
    return icons.get(name, "")

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.portfolio import _build_market_stats
from backtest.indicators import sma, stddev
from logic.v_reversal import (
    # 常量
    V_LOOKBACK, V_DECLINE_MIN, V_RECOVERY_MIN, V_RECOVERY_RATIO,
    V_MIN_DOWN_DAYS, V_MIN_UP_DAYS, V_MAX_DURATION, V_RECOVERY_HARD_MAX,
    VOL_STABLE_MAX, VOL_RATIO_BUY, VOL_RATIO_SELL,
    LIMIT_UP_PCT, LIMIT_UP_LOOKBACK,
    # 核心函数
    _compute_volatility_metrics,
    _detect_v_reversal,
    _has_recent_limit_up,
    _extract_volume_confirm,
    _extract_price_context,
)
from collections import defaultdict

# ============ 可调参数 ============

# -- 大盘评分 --
GREEN_THRESHOLD = 65       # ≥65 为 GREEN
YELLOW_THRESHOLD = 35      # ≥35 为 YELLOW, <35 为 RED
HISTORY_DAYS = 60          # 百分位计算用的历史天数
WEIGHT_BREADTH = 0.40      # 涨跌比权重
WEIGHT_RISK = 0.35         # 跌停风险权重
WEIGHT_VOLUME = 0.25       # 量能权重

# -- 横盘蓄力（来自策略常量: LIMIT_UP_PCT, LIMIT_UP_LOOKBACK, V_RECOVERY_HARD_MAX 已从策略导入）--
V_RECOVERY_SOFT_MAX = 10.0   # V反恢复超过此值 → 开始扣分（策略硬过滤是20%）
SIDEWAYS_NET_MAX = 4.0       # 5日净涨跌幅不超过此值才加分（%）
SIDEWAYS_VOL_MIN = 0.020     # 日均波动率至少2%才算有效横盘

# -- 候选股评分 --
WEIGHT_V_REVERSAL = 0.20     # V反形态质量
WEIGHT_VOL_SIGNAL = 0.22     # 波动率信号强度
WEIGHT_VOLUME_CONFIRM = 0.10 # 量能确认
WEIGHT_STABILITY = 0.13      # 稳定性溢价
WEIGHT_VOL_TREND = 0.10      # 波动率趋势
WEIGHT_SIDEWAYS = 0.25       # 横盘蓄力（高波动+低净涨跌=还没拉升）

STRONG_RECOMMEND = 60       # ≥60 重点推荐（横盘蓄力模式天然分数偏低）
WATCH_THRESHOLD = 40        # ≥40 可关注, <40 一般

# -- 展示 --
DEFAULT_TOP_N = 15          # 默认展示前N只候选
GREEN_SHOW = 10             # GREEN时展示前N只
YELLOW_SHOW = 5             # YELLOW时展示前N只
RED_SHOW = 3                # RED时展示前N只（仅供参考）


# ============ 工具函数 ============

def _parse_v_label(label: str) -> dict:
    """
    解析 V 反形态标签。

    输入: "V反(3阴-2阳|跌13.6%→涨6.5%)"
    返回: {"down_days": 3, "up_days": 2, "decline_pct": 13.6, "recovery_pct": 6.5}
    解析失败返回空dict
    """
    # 匹配 V反(N阴-M阳|跌X%→涨Y%)
    m = re.search(r'V反\((\d+)阴-(\d+)阳\|跌([\d.]+)%→涨([\d.]+)%\)', label)
    if m:
        return {
            "down_days": int(m.group(1)),
            "up_days": int(m.group(2)),
            "decline_pct": float(m.group(3)),
            "recovery_pct": float(m.group(4)),
        }
    # 兼容更简略的格式
    m2 = re.search(r'V反\((\d+)阴-(\d+)阳\|跌([\d.]+)→涨([\d.]+)', label)
    if m2:
        return {
            "down_days": int(m2.group(1)),
            "up_days": int(m2.group(2)),
            "decline_pct": float(m2.group(3)),
            "recovery_pct": float(m2.group(4)),
        }
    return {}


def _percentile(value: float, history: list, reverse: bool = False) -> float:
    """
    计算 value 在 history 中的百分位 (0-100)。

    reverse=True: 高值=低分位（用于跌停数等"越少越好"的指标）
    """
    if not history:
        return 50.0
    sorted_h = sorted(history)
    n = len(sorted_h)
    # 计算有多少个历史值 <= value
    count_le = sum(1 for v in sorted_h if v <= value)
    pct = (count_le / n) * 100
    if reverse:
        pct = 100 - pct
    return max(0.0, min(100.0, pct))


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _weekday_str(date_str: str) -> str:
    """在日期后附加星期几，如 '2026-06-04 (周四)'"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{date_str} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


# ============ Phase 1: 大盘环境评估 ============

def _build_metric_history(market_stats: dict, metric: str, n_days: int = HISTORY_DAYS) -> list:
    """
    从 market_stats 中提取最近 n_days 的某个指标历史序列。

    支持的 metric:
      - "breadth": 涨跌比
      - "limit_down": 跌停数量
      - "amount_ratio": 成交额/MA20
    """
    dates = sorted(market_stats.keys())
    if len(dates) > n_days:
        dates = dates[-n_days:]

    values = []
    for d in dates:
        item = market_stats[d]
        if metric == "breadth":
            values.append(item.get("breadth", 0.5))
        elif metric == "limit_down":
            values.append(item.get("limit_down", 0))
        elif metric == "amount_ratio":
            ma20 = item.get("amount_ma20", 0)
            amount = item.get("amount", 0)
            ratio = amount / ma20 if ma20 > 0 else 1.0
            values.append(ratio)
    return values


def assess_market(market_stats: dict, latest_date: str) -> dict:
    """
    评估大盘环境，返回评分和信号灯。

    返回:
    {
        "date": str,
        "composite_score": float (0-100),
        "signal": "GREEN" | "YELLOW" | "RED",
        "breadth_current": float,
        "breadth_score": float,
        "breadth_percentile": float,
        "limit_down_current": int,
        "risk_score": float,
        "limit_down_percentile": float,
        "amount_current": float,
        "amount_ma20": float,
        "amount_ratio": float,
        "volume_score": float,
        "volume_percentile": float,
        "summary": str,
    }
    """
    latest = market_stats.get(latest_date, {})
    if not latest:
        return {
            "date": latest_date,
            "composite_score": 0,
            "signal": "RED",
            "summary": "无法获取大盘数据",
        }

    # 提取当前值
    breadth_current = latest.get("breadth", 0.5)
    limit_down_current = latest.get("limit_down", 0)
    amount_current = latest.get("amount", 0)
    amount_ma20 = latest.get("amount_ma20", 1)
    amount_ratio = amount_current / amount_ma20 if amount_ma20 > 0 else 1.0

    # 构建历史序列
    breadth_history = _build_metric_history(market_stats, "breadth")
    limit_down_history = _build_metric_history(market_stats, "limit_down")
    amount_ratio_history = _build_metric_history(market_stats, "amount_ratio")

    # ---- 广度评分 (breadth) ----
    breadth_percentile = _percentile(breadth_current, breadth_history)
    breadth_score = breadth_percentile  # 越高越好

    # ---- 风险评分 (limit_down) ----
    # 跌停数越少越好
    limit_down_percentile = _percentile(limit_down_current, limit_down_history, reverse=True)
    # 如果跌停数 ≥ 50，封顶30分
    if limit_down_current >= 50:
        risk_score = min(30.0, limit_down_percentile)
    else:
        risk_score = limit_down_percentile

    # ---- 量能评分 (amount ratio) ----
    volume_percentile = _percentile(amount_ratio, amount_ratio_history)
    volume_score = volume_percentile

    # ---- 综合评分 ----
    composite = (
        breadth_score * WEIGHT_BREADTH
        + risk_score * WEIGHT_RISK
        + volume_score * WEIGHT_VOLUME
    )

    # ---- 信号灯 ----
    if composite >= GREEN_THRESHOLD:
        signal = "GREEN"
    elif composite >= YELLOW_THRESHOLD:
        signal = "YELLOW"
    else:
        signal = "RED"

    # ---- 摘要 ----
    signal_cn = {"GREEN": "GREEN 健康", "YELLOW": "YELLOW 一般", "RED": "RED 危险"}
    parts = [f"综合评分 {composite:.0f}/100 → {signal_cn.get(signal, signal)}"]

    if breadth_percentile < 30:
        parts.append(f"广度处于历史低位({breadth_percentile:.0f}%)")
    if limit_down_current >= 50:
        parts.append(f"跌停{limit_down_current}只，系统性风险偏高")
    if amount_ratio < 0.7:
        parts.append(f"成交额萎缩至均值的{amount_ratio*100:.0f}%")
    if signal == "GREEN":
        parts.append("大盘环境健康，可以积极参与")

    return {
        "date": latest_date,
        "composite_score": round(composite, 1),
        "signal": signal,
        "breadth_current": round(breadth_current, 3),
        "breadth_score": round(breadth_score, 1),
        "breadth_percentile": round(breadth_percentile, 1),
        "limit_down_current": limit_down_current,
        "risk_score": round(risk_score, 1),
        "limit_down_percentile": round(limit_down_percentile, 1),
        "amount_current": amount_current,
        "amount_ma20": amount_ma20,
        "amount_ratio": round(amount_ratio, 3),
        "volume_score": round(volume_score, 1),
        "volume_percentile": round(volume_percentile, 1),
        "summary": "；".join(parts),
    }


# ============ Phase 2: 候选股筛选 ============
# (_extract_price_context, _extract_volume_confirm, _has_recent_limit_up 从 logic/v_reversal 导入)

def _extract_vol_trend(daily_vol: list, idx: int) -> dict:
    """提取波动率趋势：加速/平稳/衰减"""
    if idx < 6:
        return {"trend": "stable", "ratio": 1.0}
    recent_3 = daily_vol[idx - 2:idx + 1]
    prior_3 = daily_vol[idx - 5:idx - 2]
    recent_avg = sum(recent_3) / 3 if recent_3 else 0
    prior_avg = sum(prior_3) / 3 if prior_3 else 0.001
    ratio = recent_avg / prior_avg if prior_avg > 0.0001 else 1.0
    if ratio > 1.2:
        trend = "accelerating"
    elif ratio < 0.8:
        trend = "decelerating"
    else:
        trend = "stable"
    return {"trend": trend, "ratio": round(ratio, 2)}


# (_extract_volume_confirm 和 _has_recent_limit_up 从 logic/v_reversal 导入)
def _check_buy_conditions(metrics: dict, closes: list, volumes: list, idx: int) -> dict | None:
    """
    在 index=idx 处检查买入条件。

    满足全部条件返回候选股dict，否则返回 None。

    关键过滤:
      - 近N天有涨停 → 排除（已经拉起来了，不是我们要的蓄力阶段）
      - V反恢复超过 V_RECOVERY_HARD_MAX → 排除（过度拉升）
    """
    v5 = metrics["vol_5d"][idx]
    v60 = metrics["vol_60d"][idx]
    daily_chg = metrics["daily_change"][idx]
    daily_change = metrics["daily_change"]

    if v5 is None or v60 is None:
        return None

    # 条件1: 历史平稳（60日波动率低）
    if v60 >= VOL_STABLE_MAX:
        return None

    # 条件1.5: 近N天无涨停 — 涨停股已经拉起来了，不是蓄力阶段
    if _has_recent_limit_up(daily_change, idx):
        return None

    # 条件2: 波动率放大
    vol_ratio = v5 / v60 if v60 > 0.001 else 1.0
    if vol_ratio < VOL_RATIO_BUY:
        return None

    # 条件3: V反形态
    is_v, bottom_idx, decline_pct, recovery_pct, v_label = _detect_v_reversal(closes, idx)
    if not is_v:
        return None

    # 条件3.5: V反恢复不过度 — 恢复超过20%说明已经拉起来了，排除
    if recovery_pct > V_RECOVERY_HARD_MAX:
        return None

    # 条件4: 不接飞刀
    if daily_chg <= -0.03:
        return None

    # 条件5: 放量确认
    vol_confirm = _extract_volume_confirm(volumes, bottom_idx, idx)
    if vol_confirm["ratio"] < 0.8:
        return None

    # ---- 提取详细信息 ----
    parsed = _parse_v_label(v_label)
    price_ctx = _extract_price_context(
        metrics["closes"], metrics["highs"], metrics["lows"], idx
    )
    vol_trend = _extract_vol_trend(metrics["daily_vol"], idx)

    # 计算 5日净涨跌幅（用于横盘蓄力评分）
    idx_5d_ago = max(0, idx - 4)
    close_5d_ago = closes[idx_5d_ago] if idx_5d_ago >= 0 else closes[0]
    net_5d_change = (closes[idx] - close_5d_ago) / close_5d_ago if close_5d_ago > 0 else 0
    # 5日日均波动率
    daily_vol_recent = metrics["daily_vol"][idx_5d_ago:idx + 1]
    avg_daily_vol_5d = sum(daily_vol_recent) / len(daily_vol_recent) if daily_vol_recent else 0

    return {
        # 身份
        "code": "",
        "name": "",
        "market": "",

        # V反
        "v_decline_pct": decline_pct,
        "v_recovery_pct": recovery_pct,
        "v_recovery_ratio": round(recovery_pct / max(decline_pct, 0.01), 2),
        "v_down_days": parsed.get("down_days", 0),
        "v_up_days": parsed.get("up_days", 0),
        "v_duration_days": idx - (idx - V_LOOKBACK) if bottom_idx > 0 else 0,
        "v_label": v_label,
        "v_bottom_idx": bottom_idx,

        # 波动率
        "vol_5d": round(v5, 5),
        "vol_60d": round(v60, 5),
        "vol_ratio": round(vol_ratio, 2),
        "daily_change_latest": round(daily_chg, 4),
        "daily_vol_latest": round(metrics["daily_vol"][idx], 5),
        "daily_range_latest": round(metrics["daily_range"][idx], 4),

        # 波动趋势
        "vol_trend": vol_trend["trend"],
        "vol_trend_ratio": vol_trend["ratio"],

        # 量能确认
        "vol_confirm_ratio": vol_confirm["ratio"],
        "right_vol": vol_confirm["right_vol"],
        "left_vol": vol_confirm["left_vol"],

        # 价格位置
        "price_position_20d": price_ctx["price_position_20d"],
        "price_vs_5d_high": price_ctx["price_vs_5d_high"],
        "dist_from_20d_low_pct": price_ctx["dist_from_20d_low_pct"],
        "close": price_ctx["close"],

        # 横盘蓄力指标
        "net_5d_change": round(net_5d_change, 5),
        "avg_daily_vol_5d": round(avg_daily_vol_5d, 5),
        "vol_efficiency": round(avg_daily_vol_5d / max(abs(net_5d_change), 0.001), 2),

        # V形跨度
        "v_duration": idx - (idx - V_LOOKBACK),

        # 信号原因
        "signal_reason": f"{v_label}，波动异动(v5/v60={vol_ratio:.1f})",

        # 评分（Phase 3填入）
        "score_v_reversal": 0.0,
        "score_vol_signal": 0.0,
        "score_volume_confirm": 0.0,
        "score_stability": 0.0,
        "score_vol_trend": 0.0,
        "score_sideways": 0.0,
        "score": 0.0,
        "rank": 0,
        "recommendation": "",
    }


def screen_candidates(bars_by_code: dict, stock_map: dict, latest_date: str) -> list[dict]:
    """
    扫描所有股票，找出最新一天满足买入条件的候选股。
    """
    candidates = []
    total_scanned = 0
    total_skipped = 0

    for code, bars in bars_by_code.items():
        if len(bars) < 65:
            total_skipped += 1
            continue
        total_scanned += 1

        # 验证最新日期匹配
        if bars[-1]["trade_date"] != latest_date:
            continue

        # 计算波动率指标
        metrics = _compute_volatility_metrics(bars)
        closes = metrics["closes"]
        volumes = metrics["volumes"]
        idx = len(closes) - 1  # 最新一天的索引

        # 检查买入条件
        result = _check_buy_conditions(metrics, closes, volumes, idx)
        if result is None:
            continue

        # 补充身份信息
        stock = stock_map.get(code, {"code": code, "name": code, "market": ""})
        result["code"] = code
        result["name"] = stock.get("name", code)
        result["market"] = stock.get("market", "")
        result["v_duration"] = idx - result["v_bottom_idx"] + V_LOOKBACK
        # 修正 v_duration: 从 V 左峰到当前的天数
        left_peak_idx = result["v_bottom_idx"]
        for j in range(result["v_bottom_idx"] - 1, max(0, idx - V_LOOKBACK), -1):
            if closes[j] > closes[left_peak_idx]:
                left_peak_idx = j
            else:
                break
        result["v_duration"] = idx - left_peak_idx

        candidates.append(result)

    print(f"  扫描: {total_scanned} 只 | 跳过(<65天): {total_skipped} 只 | 候选: {len(candidates)} 只")
    return candidates


# ============ Phase 3: 候选股评分排序 ============

def _score_v_reversal(c: dict) -> float:
    """
    V反形态质量评分 (0-100)。

    基于 recovery_ratio (= 恢复幅度/下跌幅度):
      - 0.4 (策略最低线) → 0分
      - 1.0 (完全恢复) → 60分
      - 2.0+ → 100分
    加分项: 阳线>阴线、快速反转
    """
    rr = c["v_recovery_ratio"]
    # 线性映射: 0.4→0, 1.0→60
    base = (rr - V_RECOVERY_RATIO) / (1.0 - V_RECOVERY_RATIO) * 60
    base = max(0, base)

    # 恢复超过跌幅 → 额外加分
    if rr > 1.0:
        bonus = min((rr - 1.0) / 1.0 * 40, 40)
        base += bonus

    # 阳线 > 阴线: +5
    if c["v_up_days"] > c["v_down_days"]:
        base += 5

    # 快速反转 (跨度 ≤ 4天): +5
    if c["v_duration"] <= 4:
        base += 5

    return _clamp(base, 0, 100)


def _score_vol_signal(c: dict) -> float:
    """
    波动率信号强度评分 (0-100)。

    vol_ratio 映射: 1.3→0, 4.0→100
    """
    vol_ratio = c["vol_ratio"]
    score = (vol_ratio - VOL_RATIO_BUY) / (4.0 - VOL_RATIO_BUY) * 100
    return _clamp(score, 0, 100)


def _score_volume_confirm(c: dict) -> float:
    """
    量能确认评分 (0-100)。

    右/左量比: 0.8→0, 2.0→100
    """
    ratio = c["vol_confirm_ratio"]
    score = (ratio - 0.8) / (2.0 - 0.8) * 100
    return _clamp(score, 0, 100)


def _score_stability(c: dict) -> float:
    """
    稳定性溢价评分 (0-100)。

    vol_60d 越低越好: 0→100, VOL_STABLE_MAX→0
    """
    v60 = c["vol_60d"]
    score = (1.0 - v60 / VOL_STABLE_MAX) * 100
    return _clamp(score, 0, 100)


def _score_vol_trend(c: dict) -> float:
    """
    波动率趋势评分 (0-100)。

    accelerating → 85, stable → 50, decelerating → 20
    乘以趋势强度系数
    """
    trend = c["vol_trend"]
    if trend == "accelerating":
        base = 85
    elif trend == "decelerating":
        base = 20
    else:
        base = 50

    # 趋势强度调整
    ratio = c["vol_trend_ratio"]
    strength = min(ratio / 1.5, 2.0) if ratio > 0 else 1.0
    return _clamp(base * strength, 0, 100)


def _score_sideways(c: dict) -> float:
    """
    横盘蓄力评分 (0-100) — 核心新增维度。

    逻辑: 高波动 + 低净涨跌 = 资金在吸筹但还没拉升 = 最佳买点。

    评分因素:
      1. 波动效率 (vol_efficiency = avg_daily_vol / |net_5d_change|):
         高波动低净涨跌 → 效率高 → 分数高
         阈值: 0.4→0, 1.5→80, 3.0+→100
      2. 扣分项: 净涨跌过大 (> SIDEWAYS_NET_MAX) → 不是横盘
      3. 扣分项: V反恢复过大 (> V_RECOVERY_SOFT_MAX) → 已经拉起来了
    """
    # 波动效率分 (核心)
    eff = c.get("vol_efficiency", 0)
    if eff >= 3.0:
        eff_score = 100
    elif eff >= 1.5:
        eff_score = 80 + (eff - 1.5) / 1.5 * 20
    elif eff >= 0.4:
        eff_score = (eff - 0.4) / 1.1 * 80
    else:
        eff_score = 0

    base = eff_score

    # 扣分: 5日净涨跌过大（超过4%说明股价已经动了）
    net_5d = abs(c.get("net_5d_change", 0)) * 100
    if net_5d > SIDEWAYS_NET_MAX:
        # 超过阈值后线性扣分，到8%扣光
        penalty = min((net_5d - SIDEWAYS_NET_MAX) / 4.0 * 100, base)
        base -= penalty

    # 扣分: V反恢复幅度过大（超过10%已经拉起来了）
    recovery = c.get("v_recovery_pct", 0)
    if recovery > V_RECOVERY_SOFT_MAX:
        # 超过10%后线性扣分，到V_RECOVERY_HARD_MAX扣光
        penalty_ratio = (recovery - V_RECOVERY_SOFT_MAX) / (V_RECOVERY_HARD_MAX - V_RECOVERY_SOFT_MAX)
        penalty = min(penalty_ratio * 80, base)
        base -= penalty

    # 加分: 日均波动率够高（至少2%）
    avg_vol = c.get("avg_daily_vol_5d", 0)
    if avg_vol >= SIDEWAYS_VOL_MIN:
        bonus = min((avg_vol - SIDEWAYS_VOL_MIN) / 0.03 * 15, 15)
        base += bonus

    return _clamp(base, 0, 100)


def score_candidates(candidates: list[dict]) -> list[dict]:
    """
    对候选股进行6维度打分排序。
    修改 candidates 并返回按 score 降序排列的列表。
    """
    for c in candidates:
        c["score_v_reversal"] = round(_score_v_reversal(c), 1)
        c["score_vol_signal"] = round(_score_vol_signal(c), 1)
        c["score_volume_confirm"] = round(_score_volume_confirm(c), 1)
        c["score_stability"] = round(_score_stability(c), 1)
        c["score_vol_trend"] = round(_score_vol_trend(c), 1)
        c["score_sideways"] = round(_score_sideways(c), 1)

        c["score"] = round(
            c["score_v_reversal"] * WEIGHT_V_REVERSAL
            + c["score_vol_signal"] * WEIGHT_VOL_SIGNAL
            + c["score_volume_confirm"] * WEIGHT_VOLUME_CONFIRM
            + c["score_stability"] * WEIGHT_STABILITY
            + c["score_vol_trend"] * WEIGHT_VOL_TREND
            + c["score_sideways"] * WEIGHT_SIDEWAYS,
            1,
        )

        if c["score"] >= STRONG_RECOMMEND:
            c["recommendation"] = "重点推荐"
        elif c["score"] >= WATCH_THRESHOLD:
            c["recommendation"] = "可关注"
        else:
            c["recommendation"] = "一般"

    # 按综合评分降序排列
    candidates.sort(key=lambda x: x["score"], reverse=True)

    for i, c in enumerate(candidates, 1):
        c["rank"] = i

    return candidates


# ============ Phase 4: 最终决策 ============

def make_decision(market: dict, ranked: list[dict]) -> dict:
    """
    根据大盘信号和候选质量，产生最终操作建议。
    """
    signal = market.get("signal", "RED")
    score = market.get("composite_score", 0)

    if signal == "GREEN":
        action = "可以积极参与"
        show_n = GREEN_SHOW
        message = (
            f"大盘环境健康（评分{score:.0f}/100），市场广度、风险、量能均处于可接受范围。\n"
            f"建议正常仓位参与，优先关注排名前{GREEN_SHOW}的候选股。"
        )
    elif signal == "YELLOW":
        action = "谨慎参与，控制仓位"
        show_n = YELLOW_SHOW
        message = (
            f"大盘环境一般（评分{score:.0f}/100），存在一些风险因素。\n"
            f"建议控制仓位不超过50%，仅关注排名前{YELLOW_SHOW}的强势候选。"
        )
    else:
        action = "建议观望，不操作"
        show_n = RED_SHOW
        message = (
            f"大盘环境危险（评分{score:.0f}/100），系统性风险较大。\n"
            f"建议暂时观望，等待市场回暖。以下候选仅供参考，不建议立即买入。"
        )

    # 风险提示
    warnings = []
    if market.get("breadth_percentile", 50) < 20:
        warnings.append("市场广度处于近60日最低20%，多数股票在下跌")
    if market.get("limit_down_current", 0) >= 50:
        warnings.append(f"跌停{market['limit_down_current']}只，恐慌情绪明显")
    if market.get("amount_ratio", 1.0) < 0.7:
        warnings.append("成交额大幅萎缩，市场流动性不足")
    warnings.append("所有信号基于历史数据，不构成投资建议，风险自担")

    recommended = ranked[:show_n]
    # 对于 RED，展示的候选标记为"参考"
    if signal == "RED":
        for c in recommended:
            c["recommendation"] = "参考（不推荐买入）"

    return {
        "market_signal": signal,
        "market_score": score,
        "action": action,
        "message": message,
        "show_n": show_n,
        "recommended": recommended,
        "all_candidates": ranked,
        "risk_warnings": warnings,
    }


# ============ 报告输出 ============

def _fmt_pct(val: float) -> str:
    """格式化百分比"""
    return f"{val * 100:.1f}%" if abs(val) < 10 else f"{val * 100:.0f}%"


def _fmt_vol_ratio(val: float) -> str:
    """格式化波动率比值"""
    return f"{val:.1f}x"


def _fmt_amount(val: float) -> str:
    """格式化成交额（亿）"""
    if val >= 1e12:
        return f"{val / 1e12:.2f}万亿"
    return f"{val / 1e8:.0f}亿"


def format_report(market: dict, ranked: list[dict], decision: dict, latest_date: str) -> str:
    """生成格式化报告文本。"""

    lines = []
    sep = "=" * 80
    sep2 = "-" * 80

    # ---- 标题 ----
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(sep)
    lines.append(f"  每日指导报告 — {_weekday_str(latest_date)}")
    lines.append(f"  生成时间: {now_str}")
    lines.append(sep)

    # ======== 一、大盘环境评估 ========
    lines.append("")
    lines.append("一、大盘环境评估")
    lines.append(sep2)

    signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
    label = signal_label.get(market.get("signal", "RED"), "[?]")
    lines.append(f"  综合评分: {market['composite_score']:.0f}/100 → {label} {market['signal']}")
    lines.append("")

    # 表格
    lines.append(f"  {'维度':<12} {'当前值':<14} {'评分':<10} {'历史分位':<10}")
    lines.append(f"  {'-' * 46}")

    breadth_str = f"{market.get('breadth_current', 0) * 100:.0f}%"
    lines.append(f"  {'涨跌比':<12} {breadth_str:<14} {market.get('breadth_score', 0):.0f}/100{'':>4} {market.get('breadth_percentile', 0):.0f}%")

    ld_str = f"{market.get('limit_down_current', 0)}只"
    lines.append(f"  {'跌停风险':<12} {ld_str:<14} {market.get('risk_score', 0):.0f}/100{'':>4} {market.get('limit_down_percentile', 0):.0f}%")

    ar = market.get('amount_ratio', 0)
    amt_str = f"{_fmt_amount(market.get('amount_current', 0))} ({ar:.2f}x)"
    lines.append(f"  {'量能':<12} {amt_str:<14} {market.get('volume_score', 0):.0f}/100{'':>4} {market.get('volume_percentile', 0):.0f}%")

    lines.append("")
    lines.append(f"  分析: {market.get('summary', 'N/A')}")

    # ======== 二、候选股扫描 ========
    lines.append("")
    lines.append("二、候选股扫描")
    lines.append(sep2)
    lines.append(f"  共发现 {len(ranked)} 只符合买入条件的候选股。")
    lines.append("")

    if not ranked:
        lines.append("  (无符合条件的候选股)")
    else:
        # 统计
        strong = sum(1 for c in ranked if c["recommendation"] == "重点推荐")
        watch = sum(1 for c in ranked if c["recommendation"] == "可关注")
        normal = sum(1 for c in ranked if c["recommendation"] == "一般")
        ref = sum(1 for c in ranked if "参考" in c["recommendation"])
        lines.append(f"  重点推荐: {strong} | 可关注: {watch} | 一般: {normal}" +
                     (f" | 参考: {ref}" if ref else ""))

        # ======== 三、候选股排名 ========
        lines.append("")
        lines.append("三、候选股排名")
        lines.append(sep2)
        lines.append("")

        # 表头
        show_n = min(len(ranked), DEFAULT_TOP_N)
        lines.append(f"  {'排名':<4} {'代码':<12} {'名称':<8} {'评分':<6} {'V反形态':<22} {'vol比':<7} {'5日净涨':<8} {'波动效率':<8}")
        lines.append(f"  {'-' * 80}")

        for c in ranked[:show_n]:
            v_summary = f"{c['v_down_days']}阴-{c['v_up_days']}阳 {c['v_recovery_pct']:.0f}%"
            lines.append(
                f"  {c['rank']:<4} "
                f"{c['code']:<12} "
                f"{c['name']:<8} "
                f"{c['score']:<6.0f} "
                f"{v_summary:<22} "
                f"{_fmt_vol_ratio(c['vol_ratio']):<7} "
                f"{c['net_5d_change']*100:+5.1f}%  "
                f"{c['vol_efficiency']:<8.1f}"
            )

        # ======== 四、重点候选详细分析 ========
        lines.append("")
        lines.append("四、重点候选详细分析")
        lines.append(sep2)

        # YELLOW/RED 时展示较少
        detail_n = min(len(ranked), max(decision["show_n"], 5))
        for c in ranked[:detail_n]:
            lines.append("")
            lines.append(f"  [{c['rank']}] {c['code']} {c['name']} — 评分{c['score']:.0f} {c['recommendation']}")
            lines.append(f"      V反形态: {c['v_down_days']}阴{c['v_up_days']}阳"
                         f" | 跌{c['v_decline_pct']:.1f}%→涨{c['v_recovery_pct']:.1f}%"
                         f" | 恢复比{c['v_recovery_ratio']:.0%}"
                         f" | V形跨度{c['v_duration']}天")
            lines.append(f"      波动率: vol_5d={_fmt_pct(c['vol_5d'])}"
                         f" | vol_60d={_fmt_pct(c['vol_60d'])}"
                         f" | ratio={_fmt_vol_ratio(c['vol_ratio'])}"
                         f" | 趋势:{c['vol_trend']}({c['vol_trend_ratio']}x)"
                         f" | 日涨跌{_fmt_pct(c['daily_change_latest'])}")
            lines.append(f"      横盘蓄力: 5日净涨跌{c['net_5d_change']*100:+.1f}%"
                         f" | 日均波动{_fmt_pct(c['avg_daily_vol_5d'])}"
                         f" | 波动效率{c['vol_efficiency']:.1f}x"
                         f" {'[优]' if c['vol_efficiency'] > 1.5 else '[一般]' if c['vol_efficiency'] > 0.5 else '[差]'}")
            lines.append(f"      量价确认: 右量/左量={c['vol_confirm_ratio']:.1f}x"
                         f" | 价格位置: 20日区间的{c['price_position_20d']*100:.0f}%"
                         f" | 距20日低+{c['dist_from_20d_low_pct']:.1f}%")
            lines.append(f"      分项评分: V反{c['score_v_reversal']:.0f}"
                         f" | 波动信号{c['score_vol_signal']:.0f}"
                         f" | 蓄力{c['score_sideways']:.0f}"
                         f" | 量能{c['score_volume_confirm']:.0f}"
                         f" | 稳定性{c['score_stability']:.0f}"
                         f" | 趋势{c['score_vol_trend']:.0f}")
            lines.append(f"      原始信号: {c['signal_reason']}")

    # ======== 五、明日操作建议 ========
    lines.append("")
    lines.append("五、明日操作建议")
    lines.append(sep2)
    lines.append("")
    lines.append(f"  {label} {decision['action']}")
    lines.append(f"  {decision['message']}")
    lines.append("")

    if decision["risk_warnings"]:
        lines.append("  风险提示:")
        for w in decision["risk_warnings"]:
            lines.append(f"    [!] {w}")

    # ======== 尾部 ========
    lines.append("")
    lines.append(sep)
    lines.append("  免责声明: 本报告基于历史数据的量化分析，不构成投资建议。")
    lines.append("  投资有风险，入市需谨慎。")
    lines.append(sep)

    return "\n".join(lines)


def format_short_report(market: dict, ranked: list[dict], decision: dict, latest_date: str) -> str:
    """
    生成简短版报告（用于QQ推送）。
    """
    signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
    label = signal_label.get(market.get("signal", "RED"), "[?]")

    lines = [
        f"[*] 每日指导报告 — {_weekday_str(latest_date)}",
        "",
        f"一、大盘: {label} {market['signal']} (评分{market['composite_score']:.0f}/100)",
        f"  广度{market.get('breadth_current', 0)*100:.0f}% | 跌停{market.get('limit_down_current', 0)}只 | 量能{market.get('amount_ratio', 0):.2f}x均量",
        f"  {market.get('summary', '')}",
        "",
    ]

    if ranked:
        top_n = min(len(ranked), decision["show_n"], 8)
        lines.append(f"二、候选 TOP{top_n}:")
        for c in ranked[:top_n]:
            lines.append(
                f"  {c['rank']}. {c['name']}({c['code']}) "
                f"评分{c['score']:.0f} "
                f"| {c['v_down_days']}阴{c['v_up_days']}阳 "
                f"| vol{c['vol_ratio']:.1f}x "
                f"| 5日净涨{c['net_5d_change']*100:+.1f}%"
            )
    else:
        lines.append("二、无符合条件候选")

    lines.append("")
    lines.append(f"三、操作: {label} {decision['action']}")
    lines.append(f"  {decision['message'][:200]}")

    return "\n".join(lines)


def save_report(report: str, latest_date: str) -> str:
    """保存报告到文件"""
    out_dir = ROOT_DIR / "data"
    os.makedirs(out_dir, exist_ok=True)
    path = out_dir / f"daily_guide_{latest_date}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return str(path)


# ============ 主流程 ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日指导报告 - 波动率V反策略")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="展示前N只候选")
    parser.add_argument("--push", action="store_true", help="推送到QQ")
    parser.add_argument("--json", action="store_true", help="同时输出JSON")
    parser.add_argument("--short", action="store_true", help="简短模式（适合推送）")
    args = parser.parse_args()

    print("=" * 80)
    print("  每日指导报告 — 波动率V反策略")
    print("=" * 80)

    # ---- 1. 数据库连接 ----
    print("\n[1/5] 连接数据库...")
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        # ---- 2. 获取最新数据 ----
        print("[2/5] 加载数据...")
        latest = sess.query(func.max(StockDaily.trade_date)).scalar()
        cnt = sess.query(func.count(StockDaily.id)).filter(StockDaily.trade_date == latest).scalar()
        print(f"  数据库最新日期: {latest}, 数据条数: {cnt}")

        # 所有活跃股票
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

        # 最近200天K线
        date_rows = (
            sess.query(StockDaily.trade_date)
            .distinct()
            .order_by(desc(StockDaily.trade_date))
            .limit(200)
            .all()
        )
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

        # ---- 3. 大盘评估 ----
        print("[3/5] 评估大盘环境...")
        market_stats = _build_market_stats(bars_by_code)
        market = assess_market(market_stats, latest)

        signal_label = {"GREEN": "[GREEN]", "YELLOW": "[YELLOW]", "RED": "[RED]"}
        print(f"  综合评分: {market['composite_score']:.0f}/100 → {signal_label.get(market['signal'], '?')} {market['signal']}")

        # 额外：原始 market_gate 判断
        # ---- 4. 候选股扫描+评分 ----
        print("[4/5] 扫描候选股...")
        candidates = screen_candidates(bars_by_code, stock_map, latest)
        ranked = score_candidates(candidates)

        # ---- 5. 决策 ----
        print("[5/5] 生成决策...")
        decision = make_decision(market, ranked)
        print(f"  操作建议: {decision['action']}")
        print(f"  推荐候选: {len(decision['recommended'])} 只")

        # ---- 输出 ----
        if args.short:
            report = format_short_report(market, ranked, decision, latest)
        else:
            report = format_report(market, ranked, decision, latest)

        print("")
        print(report)

        # 保存文件
        path = save_report(report, latest)
        print(f"\n报告已保存至: {path}")

        # JSON 输出
        if args.json:
            json_path = ROOT_DIR / "data" / f"daily_guide_{latest}.json"
            json_data = {
                "date": latest,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "market": {k: v for k, v in market.items() if k != "summary"},
                "market_summary": market.get("summary", ""),
                "decision": {
                    "action": decision["action"],
                    "signal": decision["market_signal"],
                    "message": decision["message"],
                    "risk_warnings": decision["risk_warnings"],
                },
                "candidates": [
                    {
                        "rank": c["rank"],
                        "code": c["code"],
                        "name": c["name"],
                        "score": c["score"],
                        "recommendation": c["recommendation"],
                        "v_label": c["v_label"],
                        "vol_ratio": c["vol_ratio"],
                        "vol_5d": c["vol_5d"],
                        "vol_60d": c["vol_60d"],
                        "vol_trend": c["vol_trend"],
                        "vol_confirm_ratio": c["vol_confirm_ratio"],
                        "net_5d_change": c["net_5d_change"],
                        "avg_daily_vol_5d": c["avg_daily_vol_5d"],
                        "vol_efficiency": c["vol_efficiency"],
                        "v_recovery_pct": c["v_recovery_pct"],
                        "score_breakdown": {
                            "v_reversal": c["score_v_reversal"],
                            "vol_signal": c["score_vol_signal"],
                            "volume_confirm": c["score_volume_confirm"],
                            "stability": c["score_stability"],
                            "vol_trend": c["score_vol_trend"],
                            "sideways": c["score_sideways"],
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
                    short = format_short_report(market, ranked, decision, latest)
                    result = push_long_message(short)
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
