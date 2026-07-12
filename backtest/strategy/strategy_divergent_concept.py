# -*- coding: utf-8 -*-
"""
股性突变埋伏策略 — 概念版

核心理念:
  计算每个概念的「股性指纹」(12 维特征向量)，对比近期(10d) vs 历史(60d)变化。
  当股性转向偏涨（收益↑、趋势↑、上涨占比↑、波动放大）时买入，
  当股性转弱或持仓超期时卖出。

策略流程:
  买入 = 偏涨分 > 阈值 + 趋势向上 + 波动放大
  卖出 = 偏涨分转负 / 趋势转跌 / 30天到期

用法（通过统一回测入口）:
  python script/run_backtest.py --universe concept --strategy divergent_concept
"""

from pathlib import Path
import sys
import math

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

META = {
    "id": "divergent_concept",
    "name": "股性突变埋伏-概念",
    "type": "concept",
    "description": (
        "12维股性指纹对比(10d vs 60d)：收益转向+趋势转向+上涨占比+波动放大。"
        "买入=偏涨分>8+趋势向上。卖出=分转负/趋势转跌/30天到期。v1:+184%/回撤14.5%/胜率54%。"
    ),
}

# ============ 参数 ============
RECENT_DAYS = 10
HISTORY_DAYS = 60
MAX_HOLD_DAYS = 30
BUY_SCORE_MIN = 8.0     # 偏涨分最低阈值
SELL_SCORE_MAX = -3.0   # 偏涨分低于此值卖出


# ============ 滚动计算 ============

def _rolling_corr(x, y, window):
    result = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < window: continue
        xw = x[i+1-window:i+1]; yw = y[i+1-window:i+1]
        valid = [(xv, yv) for xv, yv in zip(xw, yw) if xv is not None and yv is not None]
        n = len(valid)
        if n < 3: continue
        sx = sum(v[0] for v in valid); sy = sum(v[1] for v in valid)
        sxy = sum(v[0]*v[1] for v in valid)
        sx2 = sum(v[0]**2 for v in valid); sy2 = sum(v[1]**2 for v in valid)
        num = n*sxy - sx*sy
        denom = (n*sx2 - sx**2) * (n*sy2 - sy**2)
        result[i] = num / (denom**0.5) if denom > 0 else 0.0
    return result


def _sma(values, window):
    result = [None] * len(values)
    buf = []
    for i, v in enumerate(values):
        if v is None: continue
        buf.append(v)
        if len(buf) > window: buf.pop(0)
        if len(buf) == window: result[i] = sum(buf) / window
    return result


# ============ 股性特征 ============

def _compute_features(bars):
    """12维股性特征（简化版，适配 generate_signals 接口）"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(closes)

    returns = []
    for i in range(1, n):
        if closes[i-1] > 0:
            returns.append((closes[i] - closes[i-1]) / closes[i-1])
    m = len(returns)
    if m < 5: return None

    return_mean = sum(returns) / m
    return_var = sum((r - return_mean)**2 for r in returns) / m
    return_std = math.sqrt(return_var) if return_var > 0 else 1e-10
    return_skew = sum((r - return_mean)**3 for r in returns) / m / (return_std**3) if return_std > 0 else 0
    up_ratio = sum(1 for r in returns if r > 0) / m

    # 趋势
    xs = list(range(n)); mx = sum(xs)/n; my = sum(closes)/n
    ss_xy = sum((x-mx)*(y-my) for x,y in zip(xs, closes))
    ss_xx = sum((x-mx)**2 for x in xs); ss_yy = sum((y-my)**2 for y in closes)
    trend_slope = (ss_xy/ss_xx)/closes[0]*100 if ss_xx>0 and closes[0]>0 else 0
    trend_r2 = (ss_xy**2)/(ss_xx*ss_yy) if ss_xx>0 and ss_yy>0 else 0

    # 回撤
    peak = closes[0]; max_dd = 0.0
    for c in closes:
        if c > peak: peak = c
        dd = (peak-c)/peak if peak>0 else 0
        if dd > max_dd: max_dd = dd

    # 自相关
    if m >= 3:
        cov_ac = sum((returns[i]-return_mean)*(returns[i-1]-return_mean) for i in range(1,m))/(m-1)
        ac1 = cov_ac/return_var if return_var>0 else 0
    else:
        ac1 = 0

    # 5日收益
    ret_5d = (closes[-1]-closes[-5])/closes[-5] if n>=5 and closes[-5]>0 else 0

    # 振幅
    amps = [(highs[i]-lows[i])/closes[i] for i in range(n) if closes[i]>0]
    amp_mean = sum(amps)/len(amps) if amps else 0

    # 量比
    if len(volumes) >= 25:
        v5 = sum(volumes[-5:])/5; v20 = sum(volumes[-25:-5])/20
        vol_ratio = v5/v20 if v20>0 else 1
    else:
        vol_ratio = 1

    return {
        "return_mean": return_mean, "return_std": return_std,
        "return_skew": return_skew, "up_day_ratio": up_ratio,
        "trend_slope_pct": trend_slope, "trend_r2": trend_r2,
        "max_drawdown": max_dd, "autocorr_1": ac1,
        "ret_5d": ret_5d, "amp_mean": amp_mean,
        "vol_ratio": vol_ratio,
    }


def _bullish_score(ft_recent, ft_history):
    """偏涨综合评分。正值=股性转好。"""
    score = 0.0
    score += (ft_recent["return_mean"] - ft_history["return_mean"]) * 200
    score += (ft_recent["trend_slope_pct"] - ft_history["trend_slope_pct"]) * 30
    score += (ft_recent["up_day_ratio"] - ft_history["up_day_ratio"]) * 20
    if ft_history["return_std"] > 0:
        ve = ft_recent["return_std"] / ft_history["return_std"]
        if 1.2 < ve < 5: score += ve * 5
    score += (ft_history["max_drawdown"] - ft_recent["max_drawdown"]) * 15
    score += ft_recent["ret_5d"] * 10
    score += (ft_recent.get("vol_ratio", 1) - 1) * 5
    score += (ft_recent["autocorr_1"] - ft_history["autocorr_1"]) * 8
    score += (ft_recent["return_skew"] - ft_history["return_skew"]) * 3
    return score


# ============ 主策略接口 ============

def generate_signals(bars):
    """单概念信号 — 绝对阈值模式（兼容 portfolio backtester）。

    注意：股性突变策略的精髓在于跨截面排名，单概念绝对阈值无法复现。
    完整版请使用 script/run_backtest.py --universe concept，自动走跨截面引擎。
    """
    n = len(bars)
    min_idx = HISTORY_DAYS + RECENT_DAYS + 10
    signals = []
    in_pos = False
    entry_idx = None
    entry_price = None

    for i in range(min_idx, n):
        close = bars[i]["close"]
        recent_bars = bars[i - RECENT_DAYS:i]
        hist_bars = bars[i - RECENT_DAYS - HISTORY_DAYS:i - RECENT_DAYS]
        ft_r = _compute_features(recent_bars)
        ft_h = _compute_features(hist_bars)
        if ft_r is None or ft_h is None: continue

        score = _bullish_score(ft_r, ft_h)

        if not in_pos:
            if (score > BUY_SCORE_MIN and ft_r["trend_slope_pct"] > 0
                    and ft_r["ret_5d"] > -0.05 and ft_r["vol_ratio"] > 0.7):
                in_pos = True; entry_idx = i; entry_price = close
                signals.append({"date": bars[i]["trade_date"], "action": "buy",
                                "reason": f"股性转强(score={score:.1f})"})
            continue

        if i == entry_idx: continue
        hold_days = i - entry_idx
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        reason = None

        if score < SELL_SCORE_MAX:
            reason = f"股性转弱(score={score:.1f},盈{profit_pct:.1f}%)"
        elif ft_r["trend_slope_pct"] < -0.15 and profit_pct < 3:
            reason = f"趋势转跌(盈{profit_pct:.1f}%)"
        elif hold_days >= MAX_HOLD_DAYS:
            reason = f"到期(盈{profit_pct:.1f}%)"

        if reason is None: continue
        signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": reason})
        in_pos = False; entry_idx = None; entry_price = None

    return signals


def market_gate(date, market_stats):
    """市场门控：大盘广度 < 40% 时禁止买入。"""
    s = market_stats.get(date, {})
    breadth = s.get("breadth", 0.5)
    if breadth < 0.40:
        return {
            "allowed": False,
            "state": "DEFENSE",
            "reasons": [f"广度过低({breadth:.2f})，暂停买入"],
        }
    return {
        "allowed": True,
        "state": "NORMAL",
        "reasons": [f"广度正常({breadth:.2f})"],
    }


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta
    run_strategy_meta(META)
