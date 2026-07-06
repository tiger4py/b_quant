# -*- coding: utf-8 -*-
"""
ETF 量价背离策略 — 直接使用 Alpha042 核心逻辑，仅做 ETF 适配微调

核心理念（同 strategy_alpha042）：
  1. 量价背离：corr(high, volume, 10) < -0.25
     价涨量缩 → 筹码锁定、无人追涨 → 看多
     放量拉升 → 散户涌入 → 看空

  2. 波动率放大器：vol_10d / vol_60d ∈ [1.2, 5.0]
     高波动环境下的量价背离信号可靠，低波动僵尸 ETF 过滤

ETF 适配调整（相对个股版）：
  - 去掉涨停过滤（ETF 不会涨停）
  - 放宽 MIN_AMOUNT 到 100 万（ETF 成交额小于个股）
  - 延长 MAX_HOLD_DAYS 到 40 天（ETF 趋势更持久）

用法示例:
  python script/run_etf_backtest.py --strategy etf_alpha
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "etf_alpha",
    "name": "Alpha042-量价背离-etf",
    "type": "etf",
    "description": (
        "ETF版Alpha042：corr(high,vol,10)<-0.25+波动放大1.2x+近20日高点。"
        "卖出：量价同步corr>0.5或40天到期。不去涨停过滤，放宽流动性。"
    ),
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
PRICE_NEAR_HIGH_PCT = 0.08
CHG_5D_MIN = -0.05

# -- 流动性（ETF 版放宽到 100 万） --
MIN_AMOUNT = 1_000_000

# -- 卖出（ETF 趋势更持久，延长到 40 天） --
MAX_HOLD_DAYS = 40

# -- 市场择时 --
MARKET_GREED_BREADTH = 0.75


# ============ 滚动相关系数（同 alpha042） ============

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


# ============ 指标预计算（同 alpha042） ============

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


# ============ market_gate（同 alpha042） ============

def market_gate(date, market_stats):
    s = market_stats.get(date, {})
    breadth = s.get("breadth", 0.5)
    if breadth > MARKET_GREED_BREADTH:
        return {"allowed": False, "state": "GREED",
                "reasons": [f"过度狂热(广度{breadth:.2f})"]}
    else:
        return {"allowed": True, "state": "NORMAL",
                "reasons": [f"正常(广度{breadth:.2f})"]}


# ============ 主策略（同 alpha042，去涨停过滤） ============

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

        # ========== 买入（同 alpha042，去掉涨停过滤） ==========
        if not in_pos:
            divergence = corr_val < CORR_BUY_MAX
            vol_amplified = VOL_AMP_MIN <= vol_amp <= VOL_AMP_MAX
            near_high = close >= high_20 * (1 - PRICE_NEAR_HIGH_PCT)
            not_falling = chg_5d > CHG_5D_MIN
            liquid = amount >= MIN_AMOUNT

            if divergence and vol_amplified and near_high and not_falling and liquid:
                in_pos = True
                entry_price = close
                entry_index = i
                corr_sign = "缩量" if corr_val < 0 else "弱同步"
                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": (
                        f"ETF Alpha({corr_sign}新高 corr={corr_val:.2f} | "
                        f"波动放大{vol_amp:.1f}x | "
                        f"距20日高{(close/high_20-1)*100:.1f}% | "
                        f"5日{chg_5d*100:.1f}%)"
                    ),
                })
            continue

        # ========== 卖出（同 alpha042） ==========
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


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
