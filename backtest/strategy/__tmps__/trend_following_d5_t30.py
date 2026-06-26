# -*- coding: utf-8 -*-
"""
趋势跟随 + 量价齐升策略 (Trend Following with Volume Confirmation)

核心理念：寻找中长期趋势向上、近期放量加速的强势股。
  1. 趋势过滤：5日/20日涨幅、40日最大回撤限制 → 排除底部反弹
  2. 量能确认：量比放大 + 量能加速 + 量价配合（涨放量/跌缩量）
  3. 位置过滤：站上MA10+MA20 + 接近20日高点 + 不追高

策略流程：
  买入 = 趋势向上 + 量价齐升 + 位置合理
  卖出 = 止盈(+25%)/持仓15天到期

精简说明：v1→v2 去掉跌破MA20/量价背离/高位回撤/量能崩塌，收益从+11%→+41%。
v2→v3 进一步去掉硬止损/单日暴跌，只保留止盈+持仓到期，测试趋势跟随的极限收益。

与扫描脚本 scan_trend_only.py 共享核心逻辑。
"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "trend_following_d5_t30",
    "name": "趋势跟随",
    "description": "趋势向上+量价齐升：5日涨>1%、20日趋势不差、40日回撤<20%、量比>1.3、涨跌量比>1.1、站上MA20、接近20日高点。次日收盘价买入。卖出：止损-8%/止盈+25%/持仓15天到期。",
}

# ============ 可调参数 ============

# -- 趋势过滤 --
PRICE_UP_5D_MIN = 0.01       # 5日涨至少1%
PRICE_UP_20D_MIN = -0.08     # 20日跌幅不超过8%
PRICE_DOWN_40D_MAX = -0.20   # 40日最大回撤不超过20%

# -- 位置过滤 --
CLOSE_ABOVE_MA20 = True      # 收盘必须站上MA20
PRICE_NEAR_20D_HIGH = 0.15   # 距20日高点 ≤ 15%
PRICE_ABOVE_MA20_MAX = 1.15  # 收盘不超过MA20的15%

# -- 量能过滤 --
VOL_RATIO_BUY = 1.3          # vol_5d / vol_20d ≥ 1.3
VOL_RATIO_MAX = 5.0          # 不超过5倍（异常爆量排除）
VOL_TREND_ACCEL = 1.0        # 近3日量 / 前3日量 ≥ 1.0（至少不萎缩）

# -- 量价配合 --
UP_VOL_RATIO = 1.1           # 上涨日平均量 / 下跌日平均量 ≥ 1.1
LOOKBACK_DAYS = 10           # 量价配合评估窗口

# -- 动量 --
MIN_CONSEC_UP = 1            # 至少1天上涨

# -- 卖出 --
STOP_LOSS_PCT = -8           # 硬止损：相对买入价跌超此值离场（%）
TAKE_PROFIT_PCT = 25         # 止盈（%）
MAX_HOLD_DAYS = 15           # 最大持仓天数
DAILY_CRASH_PCT = -8         # 单日暴跌超过此值离场（%）


# ============ 辅助函数 ============

def _compute_price_volume_dynamics(closes, volumes, i, lookback=LOOKBACK_DAYS):
    """计算最近N天的量价配比（涨时放量 vs 跌时缩量）"""
    start = max(1, i - lookback + 1)
    up_vols, down_vols = [], []
    up_days = down_days = 0

    for j in range(start, i + 1):
        chg = closes[j] - closes[j - 1]
        vol = volumes[j] if volumes[j] else 0
        if chg > 0:
            up_vols.append(vol)
            up_days += 1
        elif chg < 0:
            down_vols.append(vol)
            down_days += 1

    up_avg = sum(up_vols) / len(up_vols) if up_vols else 0
    down_avg = sum(down_vols) / len(down_vols) if down_vols else 1
    up_vol_ratio = up_avg / down_avg if down_avg > 0 else 2.0

    # 连续上涨天数
    consecutive_up = 0
    for j in range(i, 0, -1):
        if closes[j] > closes[j - 1]:
            consecutive_up += 1
        else:
            break

    return {
        "up_vol_ratio": round(up_vol_ratio, 2),
        "up_days": up_days,
        "down_days": down_days,
        "consecutive_up": consecutive_up,
    }


def _window_max_drawdown_pct(prices):
    """按时间顺序计算窗口内最大回撤（负小数，如 -0.25 表示-25%）。"""
    if not prices:
        return 0.0

    peak = prices[0]
    max_drawdown = 0.0
    for price in prices[1:]:
        if price > peak:
            peak = price
        if peak > 0:
            drawdown = (price - peak) / peak
            if drawdown < max_drawdown:
                max_drawdown = drawdown
    return max_drawdown


# ============ 主策略 ============

def generate_signals(bars):
    """
    生成买卖信号。

    买入 = 趋势向上 + 量价齐升 + 位置合理
    卖出 = 止盈(+25%)/持仓15天到期
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(closes)

    # 预计算指标
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    min_idx = 42  # 40日回撤需要40根 + buffer

    for i in range(min_idx, n):
        close = closes[i]

        # 检查指标有效性
        if ma10[i] is None or ma20[i] is None:
            continue
        if vol_ma5[i] is None or vol_ma20[i] is None:
            continue
        if vol_ma20[i] == 0:
            continue

        vol_ratio = vol_ma5[i] / vol_ma20[i]
        daily_chg = (closes[i] - closes[i - 1]) / closes[i - 1] if i > 0 and closes[i - 1] > 0 else 0

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # ---- 趋势过滤 ----
            # 5日涨幅
            if closes[i - 5] <= 0:
                continue
            chg_5d = (close - closes[i - 5]) / closes[i - 5]
            if chg_5d < PRICE_UP_5D_MIN:
                continue

            # 20日涨幅
            chg_20d = (close - closes[i - 20]) / closes[i - 20] if closes[i - 20] > 0 else 0
            if chg_20d < PRICE_UP_20D_MIN:
                continue

            # 40日最大回撤
            lookback_40 = closes[i - 39:i + 1]
            max_dd_40 = _window_max_drawdown_pct(lookback_40)
            if max_dd_40 < PRICE_DOWN_40D_MAX:
                continue

            # ---- 位置过滤 ----
            if close <= ma10[i] or close <= ma20[i]:
                continue

            high_20 = max(highs[i - 19:i + 1])
            if (high_20 - close) / high_20 > PRICE_NEAR_20D_HIGH:
                continue

            if close > ma20[i] * PRICE_ABOVE_MA20_MAX:
                continue

            # ---- 量能过滤 ----
            if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
                continue

            # 量能加速
            if i >= 6:
                recent_3 = sum(volumes[i - 2:i + 1]) / 3
                prior_3 = sum(volumes[i - 5:i - 2]) / 3
                if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
                    continue

            # ---- 量价配合 ----
            dyn = _compute_price_volume_dynamics(closes, volumes, i)
            if dyn["up_vol_ratio"] < UP_VOL_RATIO:
                continue
            if dyn["consecutive_up"] < MIN_CONSEC_UP:
                continue

            # ==== 全部条件满足 → 买入（次日收盘价成交，不含信号日收益）====
            if i + 1 >= n:
                continue  # 最后一天不买入，没有次日价格

            # 次日一字涨停无法买入 → 跳过
            next_high = highs[i + 1]
            next_low = lows[i + 1]
            next_close = closes[i + 1]
            next_chg = (next_close - close) / close if close > 0 else 0
            if next_high == next_low and next_chg >= 0.095:
                continue  # 一字涨停，买不到

            # 入场日确认：站上MA10/MA20（防止信号日满足但次日跌破）
            if ma10[i + 1] is None or ma20[i + 1] is None:
                continue
            if next_close <= ma10[i + 1] or next_close <= ma20[i + 1]:
                continue

            in_pos = True
            entry_price = next_close
            entry_index = i + 1

            signals.append({
                "date": bars[i + 1]["trade_date"],  # 实际成交日期
                "action": "buy",
                "reason": (
                    f"趋势跟随(5日{chg_5d*100:.1f}% 20日{chg_20d*100:.1f}% | "
                    f"量比{vol_ratio:.1f}x 涨跌量比{dyn['up_vol_ratio']:.1f}x | "
                    f"连涨{dyn['consecutive_up']}天)"
                ),
            })
            continue

        # ==================== 卖出逻辑 ====================
        # v5: 硬止损 + 止盈 + 单日暴跌 + 早期退出(3日跌>2%) + 持仓到期
        if i == entry_index:
            continue  # 入场当天不卖出（T+1，最早次日才能卖）

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        reason = None

        # 1. 硬止损
        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"

        # 2. 止盈
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = f"止盈({profit_pct:.1f}%)"

        # 3. 单日暴跌
        elif daily_chg <= DAILY_CRASH_PCT / 100:
            reason = f"单日暴跌({daily_chg:.1%})"

        # 4. 早期退出：第3天起跌幅>2% → 提前止损（避免扛到-8%）
        elif hold_days >= 5 and profit_pct <= -3:
            reason = f"早期退出(持仓{hold_days}天跌{profit_pct:.1f}%)"

        # 5. 持仓过久
        elif hold_days >= MAX_HOLD_DAYS:
            reason = f"持仓{hold_days}天到期"

        if reason is None:
            continue

        signals.append({
            "date": bars[i]["trade_date"],
            "action": "sell",
            "reason": reason,
        })
        in_pos = False
        entry_price = None
        entry_index = None

    return signals


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
