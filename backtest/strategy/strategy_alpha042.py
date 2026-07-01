# -*- coding: utf-8 -*-
"""
Alpha #042 量价背离策略 — 缩量新高 + 波动率放大

核心理念（国泰君安191因子库 #042）：
  1. 量价背离：corr(high, volume, 10) < -0.25
     缩量创新高（价涨量缩）→ 筹码锁定、无人追涨 → 看多
     放量拉升（价量同步）→ 散户涌入、主力出货 → 看空

  2. 波动率放大器：vol_10d / vol_60d ∈ [1.2, 5.0]
     高波动环境下的量价背离信号可靠，低波动僵尸股过滤

策略流程：
  买入 = 量价背离 + 波动率放大 + 位置确认(近20日高点) + 不接飞刀 + 不追涨停
  卖出 = 量价同步(corr>0.5)离场 / 30天到期。不做硬止损/止盈。

V4回测 (2022~2026.6): +181.83% 收益, 28.10% 回撤, 48.88% 胜率, 1.36 PF
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "alpha042",
    "name": "Alpha042量价背离",
    "description": "国泰君安191#042：corr(high,vol,10)<-0.25+波动放大1.2x+近20日高点。卖出：量价同步corr>0.5或30天到期。不止损。V4:+182%/回撤28%/胜率49%。",
}

# ============ 可调参数 ============

# -- 量价背离 --
CORR_WINDOW = 10
CORR_BUY_MAX = -0.25
CORR_SELL_THRESH = 0.50

# -- 波动率放大器 --
VOL_SHORT = 10
VOL_LONG = 60
VOL_AMP_MIN = 1.20
VOL_AMP_MAX = 5.0

# -- 位置确认 --
PRICE_NEAR_HIGH_LOOKBACK = 20
PRICE_NEAR_HIGH_PCT = 0.10
CHG_5D_MIN = -0.05

# -- 涨停过滤 --
LIMIT_UP_PCT = 9.5
LIMIT_UP_LOOKBACK = 3

# -- 流动性 --
MIN_AMOUNT = 5_000_000

# -- 卖出 --
MAX_HOLD_DAYS = 30

# -- 市场择时 --
MARKET_GREED_BREADTH = 0.80


# ============ 滚动相关系数 ============

def _rolling_corr(x, y, window):
    result = [None] * len(x)
    for i in range(len(x)):
        if i + 1 < window:
            continue
        x_win = x[i + 1 - window:i + 1]
        y_win = y[i + 1 - window:i + 1]
        valid = [(xv, yv) for xv, yv in zip(x_win, y_win)
                 if xv is not None and yv is not None]
        n = len(valid)
        if n < 3:
            continue
        sum_x = sum(v[0] for v in valid)
        sum_y = sum(v[1] for v in valid)
        sum_xy = sum(v[0] * v[1] for v in valid)
        sum_x2 = sum(v[0] ** 2 for v in valid)
        sum_y2 = sum(v[1] ** 2 for v in valid)
        num = n * sum_xy - sum_x * sum_y
        inner = (n * sum_x2 - sum_x ** 2) * (n * sum_y2 - sum_y ** 2)
        if inner <= 0:
            result[i] = 0.0
        else:
            result[i] = num / (inner ** 0.5)
    return result


def _rolling_max(values, window):
    result = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < window:
            continue
        result[i] = max(values[i + 1 - window:i + 1])
    return result


# ============ 指标预计算 ============

def _compute_metrics(bars):
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    amounts = [b.get("amount") or 0 for b in bars]
    n = len(bars)

    daily_change = [0.0]
    daily_vol = [0.0]
    for i in range(1, n):
        chg = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0
        daily_change.append(chg)
        daily_vol.append(abs(chg))

    vol_short = sma(daily_vol, VOL_SHORT)
    vol_long = sma(daily_vol, VOL_LONG)

    vol_amp = [None] * n
    for i in range(n):
        if vol_short[i] is not None and vol_long[i] is not None and vol_long[i] > 0.0001:
            vol_amp[i] = vol_short[i] / vol_long[i]

    high_vol_corr = _rolling_corr(highs, volumes, CORR_WINDOW)
    high_20 = _rolling_max(highs, PRICE_NEAR_HIGH_LOOKBACK)

    chg_5d = [None] * n
    for i in range(n):
        if i >= 5 and closes[i - 5] > 0:
            chg_5d[i] = (closes[i] - closes[i - 5]) / closes[i - 5]

    return {
        "closes": closes, "highs": highs, "lows": lows,
        "volumes": volumes, "amounts": amounts,
        "daily_change": daily_change, "daily_vol": daily_vol,
        "vol_short": vol_short, "vol_long": vol_long, "vol_amp": vol_amp,
        "high_vol_corr": high_vol_corr, "high_20": high_20, "chg_5d": chg_5d,
    }


# ============ 辅助函数 ============

def _has_recent_limit_up(daily_change, i, lookback=LIMIT_UP_LOOKBACK):
    start = max(1, i - lookback + 1)
    for j in range(start, i + 1):
        if daily_change[j] * 100 >= LIMIT_UP_PCT:
            return True
    return False


# ============ market_gate ============

def market_gate(date, market_stats):
    s = market_stats.get(date, {})
    breadth = s.get("breadth", 0.5)
    if breadth > MARKET_GREED_BREADTH:
        return {"allowed": False, "state": "GREED",
                "reasons": [f"过度狂热(广度{breadth:.2f})"]}
    else:
        return {"allowed": True, "state": "NORMAL",
                "reasons": [f"正常(广度{breadth:.2f})"]}


# ============ 主策略 ============

def generate_signals(bars):
    m = _compute_metrics(bars)
    closes = m["closes"]
    n = len(closes)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    min_idx = max(VOL_LONG, PRICE_NEAR_HIGH_LOOKBACK, CORR_WINDOW) + 5

    for i in range(min_idx, n):
        close = closes[i]
        corr_val = m["high_vol_corr"][i]
        vol_amp = m["vol_amp"][i]
        high_20 = m["high_20"][i]
        chg_5d = m["chg_5d"][i]

        if corr_val is None or vol_amp is None or high_20 is None:
            continue
        if chg_5d is None:
            continue

        amount = m["amounts"][i]

        # ========== 买入 ==========
        if not in_pos:
            divergence = corr_val < CORR_BUY_MAX
            vol_amplified = VOL_AMP_MIN <= vol_amp <= VOL_AMP_MAX
            near_high = close >= high_20 * (1 - PRICE_NEAR_HIGH_PCT)
            not_falling = chg_5d > CHG_5D_MIN
            no_limit_up = not _has_recent_limit_up(m["daily_change"], i)
            liquid = amount >= MIN_AMOUNT

            if divergence and vol_amplified and near_high and not_falling and no_limit_up and liquid:
                in_pos = True
                entry_price = close
                entry_index = i
                corr_sign = "缩量" if corr_val < 0 else "弱同步"
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": (
                        f"Alpha042({corr_sign}新高 corr={corr_val:.2f} | "
                        f"波动放大{vol_amp:.1f}x | "
                        f"距20日高{(close/high_20-1)*100:.1f}% | "
                        f"5日{chg_5d*100:.1f}%)"
                    ),
                })
            continue

        # ========== 卖出 ==========
        if i == entry_index:
            continue

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        reason = None

        if corr_val > CORR_SELL_THRESH:
            reason = f"量价同步(散户涌入 corr={corr_val:.2f},盈{profit_pct:.1f}%)"
        elif hold_days >= MAX_HOLD_DAYS:
            reason = f"持仓{hold_days}天到期(盈{profit_pct:.1f}%)"

        if reason is None:
            continue

        signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": reason})
        in_pos = False
        entry_price = None
        entry_index = None

    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta
    run_strategy_meta(META)
