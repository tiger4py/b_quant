"""波动率V反检测 — 共享模块

从 daily_guide.py 抽取，供 daily_review.py 等复用。

提供:
  - V反检测常量
  - _compute_volatility_metrics(bars) → 波动率指标
  - _detect_v_reversal(closes, i) → V反形态检测
  - _check_buy_conditions(metrics, closes, volumes, idx) → 买入条件检查
  - screen_v_reversal_candidates(bars_by_code, stock_map, latest_date) → 全市场扫描
"""
from backtest.indicators import sma, stddev


# ============ V反检测常量 ============

V_LOOKBACK = 8
V_DECLINE_MIN = 3.0
V_RECOVERY_MIN = 1.5
V_RECOVERY_RATIO = 0.4
V_MIN_DOWN_DAYS = 2
V_MIN_UP_DAYS = 1
V_MAX_DURATION = 8
V_RECOVERY_HARD_MAX = 20.0

# 波动率
VOL_STABLE_MAX = 0.025
VOL_RATIO_BUY = 1.3
VOL_RATIO_SELL = 0.8

# 涨停过滤
LIMIT_UP_PCT = 9.5
LIMIT_UP_LOOKBACK = 3


# ============ 波动率指标计算 ============

def _compute_volatility_metrics(bars):
    """预计算所有波动率相关指标。"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]

    n = len(bars)

    daily_vol = [0.0]
    daily_change = [0.0]
    for i in range(1, n):
        chg = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0
        daily_vol.append(abs(chg))
        daily_change.append(chg)

    daily_range = [(highs[i] - lows[i]) / closes[i] if closes[i] else 0 for i in range(n)]

    return {
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volumes": volumes,
        "daily_vol": daily_vol,
        "daily_change": daily_change,
        "daily_range": daily_range,
        "vol_5d": sma(daily_vol, 5),
        "vol_10d": sma(daily_vol, 10),
        "vol_20d": sma(daily_vol, 20),
        "vol_60d": sma(daily_vol, 60),
        "vol_std_10d": stddev(daily_vol, 10),
        "range_ma10": sma(daily_range, 10),
        "vol_ma5": sma(volumes, 5),
        "vol_ma20": sma(volumes, 20),
    }


# ============ V反形态检测 ============

def _detect_v_reversal(closes, i, lookback=V_LOOKBACK):
    """在 index=i 处检测 V 形反转形态。"""
    if i < 4:
        return False, -1, 0, 0, ""

    start = max(0, i - lookback)
    window_for_min = closes[start:i]
    if not window_for_min:
        return False, -1, 0, 0, ""

    min_val = min(window_for_min)
    bottom_idx = start + window_for_min.index(min_val)
    bottom_close = closes[bottom_idx]

    left_peak_idx = bottom_idx
    for j in range(bottom_idx - 1, start, -1):
        if closes[j] > closes[left_peak_idx]:
            left_peak_idx = j
        else:
            break
    pre_high = closes[left_peak_idx]

    decline_pct = (pre_high - bottom_close) / pre_high * 100 if pre_high > 0 else 0
    if decline_pct < V_DECLINE_MIN:
        return False, -1, 0, 0, ""

    recovery_pct = (closes[i] - bottom_close) / bottom_close * 100 if bottom_close > 0 else 0
    if recovery_pct < V_RECOVERY_MIN:
        return False, -1, 0, 0, ""
    if recovery_pct < decline_pct * V_RECOVERY_RATIO:
        return False, -1, 0, 0, ""

    down_days = 0
    for j in range(bottom_idx, left_peak_idx, -1):
        if closes[j] < closes[j - 1] and (closes[j - 1] / closes[j] - 1) >= 0.005:
            down_days += 1
    if down_days < V_MIN_DOWN_DAYS:
        return False, -1, 0, 0, ""

    up_days = 0
    for j in range(bottom_idx + 1, i + 1):
        if closes[j] > closes[j - 1]:
            up_days += 1
    if up_days < V_MIN_UP_DAYS:
        return False, -1, 0, 0, ""

    v_duration = i - left_peak_idx
    if v_duration > V_MAX_DURATION:
        return False, -1, 0, 0, ""

    if bottom_idx > start and closes[bottom_idx - 1] < bottom_close:
        return False, -1, 0, 0, ""
    if bottom_idx < i - 1 and closes[bottom_idx + 1] < bottom_close:
        return False, -1, 0, 0, ""

    label = f"V反({down_days}阴-{up_days}阳|跌{decline_pct:.1f}%→涨{recovery_pct:.1f}%)"
    return True, bottom_idx, round(decline_pct, 2), round(recovery_pct, 2), label


# ============ 辅助检测函数 ============

def _has_recent_limit_up(daily_change, idx, lookback=LIMIT_UP_LOOKBACK):
    """检查近N天内是否有涨停（含当日）"""
    start = max(1, idx - lookback + 1)
    for j in range(start, idx + 1):
        if daily_change[j] * 100 >= LIMIT_UP_PCT:
            return True
    return False


def _extract_volume_confirm(volumes, bottom_idx, current_idx):
    """计算V反右侧/左侧量比"""
    span = current_idx - bottom_idx
    if span < 1:
        span = 1
    right_vols = volumes[bottom_idx:current_idx + 1]
    right_avg = sum(v for v in right_vols if v) / max(len(right_vols), 1)

    left_start = max(0, bottom_idx - span)
    left_vols = volumes[left_start:bottom_idx + 1]
    left_avg = sum(v for v in left_vols if v) / max(len(left_vols), 1)

    ratio = right_avg / left_avg if left_avg > 0 else 1.0
    return {
        "right_vol": round(right_avg, 0),
        "left_vol": round(left_avg, 0),
        "ratio": round(ratio, 2),
    }


def _extract_price_context(closes, highs, lows, idx):
    """提取价格位置信息"""
    close = closes[idx]
    start_20 = max(0, idx - 19)
    window_highs = highs[start_20:idx + 1]
    window_lows = lows[start_20:idx + 1]
    h20 = max(window_highs) if window_highs else close
    l20 = min(window_lows) if window_lows else close

    range_20 = h20 - l20
    position = (close - l20) / range_20 if range_20 > 0 else 0.5

    start_5 = max(0, idx - 4)
    h5 = max(highs[start_5:idx + 1]) if highs[start_5:idx + 1:] else close

    return {
        "price_position_20d": round(position, 3),
        "price_vs_5d_high": round(close / h5, 3) if h5 > 0 else 1.0,
        "price_vs_20d_high": round(close / h20, 3) if h20 > 0 else 1.0,
        "dist_from_20d_low_pct": round((close - l20) / l20 * 100, 1) if l20 > 0 else 0,
        "close": close,
        "high_20d": h20,
        "low_20d": l20,
    }


# ============ 买入条件检查 ============

def _check_buy_conditions(metrics, closes, volumes, idx):
    """
    在 index=idx 处检查买入条件。

    满足全部条件返回候选股dict，否则返回 None。
    """
    import re

    v5 = metrics["vol_5d"][idx]
    v60 = metrics["vol_60d"][idx]
    daily_chg = metrics["daily_change"][idx]
    daily_change = metrics["daily_change"]

    if v5 is None or v60 is None:
        return None

    # 条件1: 历史平稳（60日波动率低）
    if v60 >= VOL_STABLE_MAX:
        return None

    # 条件1.5: 近N天无涨停
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

    # 条件3.5: V反恢复不过度
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
    # 解析V反标签
    parsed = {}
    m = re.search(r'V反\((\d+)阴-(\d+)阳\|跌([\d.]+)%→涨([\d.]+)%\)', v_label)
    if m:
        parsed = {
            "down_days": int(m.group(1)),
            "up_days": int(m.group(2)),
            "decline_pct": float(m.group(3)),
            "recovery_pct": float(m.group(4)),
        }

    price_ctx = _extract_price_context(
        metrics["closes"], metrics["highs"], metrics["lows"], idx
    )

    # 波动趋势
    daily_vol = metrics["daily_vol"]
    vol_trend_ratio = 1.0
    vol_trend = "stable"
    if idx >= 6:
        recent_3 = daily_vol[idx - 2:idx + 1]
        prior_3 = daily_vol[idx - 5:idx - 2]
        recent_avg = sum(recent_3) / 3 if recent_3 else 0
        prior_avg = sum(prior_3) / 3 if prior_3 else 0.001
        vol_trend_ratio = recent_avg / prior_avg if prior_avg > 0.0001 else 1.0
        if vol_trend_ratio > 1.2:
            vol_trend = "accelerating"
        elif vol_trend_ratio < 0.8:
            vol_trend = "decelerating"

    # 5日净涨跌幅
    closes_all = metrics["closes"]
    idx_5d_ago = max(0, idx - 4)
    close_5d_ago = closes_all[idx_5d_ago] if idx_5d_ago >= 0 else closes_all[0]
    net_5d_change = (closes_all[idx] - close_5d_ago) / close_5d_ago if close_5d_ago > 0 else 0
    daily_vol_recent = daily_vol[idx_5d_ago:idx + 1]
    avg_daily_vol_5d = sum(daily_vol_recent) / len(daily_vol_recent) if daily_vol_recent else 0

    # V形跨度
    left_peak_idx = bottom_idx
    for j in range(bottom_idx - 1, max(0, idx - V_LOOKBACK), -1):
        if closes_all[j] > closes_all[left_peak_idx]:
            left_peak_idx = j
        else:
            break
    v_duration = idx - left_peak_idx

    return {
        "code": "",
        "name": "",
        "market": "",

        # V反
        "v_decline_pct": decline_pct,
        "v_recovery_pct": recovery_pct,
        "v_recovery_ratio": round(recovery_pct / max(decline_pct, 0.01), 2),
        "v_down_days": parsed.get("down_days", 0),
        "v_up_days": parsed.get("up_days", 0),
        "v_label": v_label,
        "v_bottom_idx": bottom_idx,
        "v_duration": v_duration,

        # 波动率
        "vol_5d": round(v5, 5),
        "vol_60d": round(v60, 5),
        "vol_ratio": round(vol_ratio, 2),
        "daily_change_latest": round(daily_chg, 4),
        "daily_vol_latest": round(daily_vol[idx], 5),
        "daily_range_latest": round(metrics["daily_range"][idx], 4),

        # 波动趋势
        "vol_trend": vol_trend,
        "vol_trend_ratio": round(vol_trend_ratio, 2),

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

        # 信号原因
        "signal_reason": f"{v_label}，波动异动(v5/v60={vol_ratio:.1f})",
    }


# ============ 全市场V反扫描 ============

def screen_v_reversal_candidates(bars_by_code, stock_map, latest_date):
    """扫描全市场，找出最新一天满足V反买入条件的候选股。"""
    candidates = []
    total_scanned = 0
    total_skipped = 0

    for code, bars in bars_by_code.items():
        if len(bars) < 65:
            total_skipped += 1
            continue
        total_scanned += 1

        if bars[-1]["trade_date"] != latest_date:
            continue

        metrics = _compute_volatility_metrics(bars)
        closes = metrics["closes"]
        volumes = metrics["volumes"]
        idx = len(closes) - 1

        result = _check_buy_conditions(metrics, closes, volumes, idx)
        if result is None:
            continue

        stock = stock_map.get(code, {"code": code, "name": code, "market": ""})
        result["code"] = code
        result["name"] = stock.get("name", code)
        result["market"] = stock.get("market", "")
        candidates.append(result)

    print(f"  [V反] 扫描: {total_scanned} 只 | 跳过(<65天): {total_skipped} 只 | 候选: {len(candidates)} 只")
    return candidates
