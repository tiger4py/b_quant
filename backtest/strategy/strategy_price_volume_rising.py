# -*- coding: utf-8 -*-
"""
价增量增策略 (Price & Volume Rising Strategy)

核心理念：量价齐升 = 资金进场信号
  1. 价增：短期价格趋势向上，连续阳线，站稳短期均线之上
  2. 量增：成交量持续放大，近5日均量 > 近20日均量，且上升日放量 > 下跌日缩量
  3. 量价配合：价涨量增、价跌量缩 → 健康上涨；价涨量缩 → 动能不足

策略流程：
  买入 = 价格趋势向上 + 量能放大 + 量价健康配合 + 不追高
  卖出 = 止损 / 止盈 / 量价背离 / 趋势破坏

关键指标：
  - price_trend_5d: 5日价格涨跌幅
  - vol_ratio: 5日均量 / 20日均量
  - up_vol_ratio: 上涨日平均量 / 下跌日平均量
  - vol_trend: 近3日均量 / 前3日均量（量能加速）
"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma

META = {
    "id": "price_volume_rising",
    "name": "价增量增",
    "description": "量价齐升：价格趋势向上+成交量放大+量价健康配合。捕捉主力资金进场信号，排除价涨量缩的弱势反弹。",
}

# ============ 可调参数 ============

# -- 价格趋势 --
PRICE_UP_5D_MIN = 0.01       # 5日涨幅至少 1%（价格在涨）
PRICE_UP_10D_MIN = 0.00      # 10日涨幅至少 0%（不处于下跌趋势）
CLOSE_ABOVE_MA10 = True      # 收盘价必须在 MA10 之上

# -- 量能放大 --
VOL_RATIO_BUY = 1.5          # vol_5d / vol_20d ≥ 1.5（量能放大确认）
VOL_RATIO_MAX = 4.0          # vol_5d / vol_20d ≤ 4.0（排除异常爆量）
VOL_TREND_ACCEL = 1.1        # 近3日均量 / 前3日均量 ≥ 1.1（量能在加速）

# -- 量价配合 --
UP_VOL_RATIO = 1.2           # 上涨日平均量 / 下跌日平均量 ≥ 1.2（涨时放量）
LOOKBACK_DAYS = 10           # 量价配合评估窗口

# -- 追高风险控制 --
PRICE_ABOVE_MA10_MAX = 1.06  # 收盘价不超过 MA10 的 6%（不追高）
MIN_CONSEC_UP = 2            # 至少连续 2 天上涨

# -- 卖出 --
STOP_LOSS_PCT = -7           # 硬止损（%）
TAKE_PROFIT_PCT = 12         # 止盈（%）
MAX_HOLD_DAYS = 8            # 最大持仓天数
DAILY_CRASH_PCT = -8         # 单日暴跌离场（%）


# ============ 量价配合计算 ============

def _compute_price_volume_dynamics(closes, volumes, i, lookback=LOOKBACK_DAYS):
    """
    计算最近 N 天的量价配比。

    返回:
      up_vol_ratio: 上涨日平均量 / 下跌日平均量（>1 = 涨时放量）
      up_days: 上涨天数
      down_days: 下跌天数
      consecutive_up: 连续上涨天数（从 i 往前数）
    """
    start = max(1, i - lookback + 1)
    up_vols = []
    down_vols = []
    up_days = 0
    down_days = 0

    for j in range(start, i + 1):
        chg = closes[j] - closes[j - 1]
        vol = volumes[j] if volumes[j] else 0
        if chg > 0:
            up_vols.append(vol)
            up_days += 1
        elif chg < 0:
            down_vols.append(vol)
            down_days += 1
        # chg == 0 不计入任何一方

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


# ============ 主策略 ============

def generate_signals(bars):
    """
    生成买卖信号。

    买入 = 价格趋势向上 + 量能放大 + 量价健康配合 + 不追高
    卖出 = 止损/止盈/量价背离/趋势破坏
    """
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(closes)

    # 预计算均线
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    # 5日涨幅
    price_chg_5d = [None] * n
    for i in range(5, n):
        if closes[i - 5] > 0:
            price_chg_5d[i] = (closes[i] - closes[i - 5]) / closes[i - 5]

    # 10日涨幅
    price_chg_10d = [None] * n
    for i in range(10, n):
        if closes[i - 10] > 0:
            price_chg_10d[i] = (closes[i] - closes[i - 10]) / closes[i - 10]

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    min_idx = 25

    for i in range(min_idx, n):
        close = closes[i]

        # 检查指标有效性
        if ma10[i] is None or ma20[i] is None:
            continue
        if vol_ma5[i] is None or vol_ma20[i] is None:
            continue
        if price_chg_5d[i] is None:
            continue
        if vol_ma20[i] == 0:
            continue

        vol_ratio = vol_ma5[i] / vol_ma20[i]
        daily_chg = (closes[i] - closes[i - 1]) / closes[i - 1] if i > 0 and closes[i - 1] > 0 else 0

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # 条件1: 价格趋势向上
            price_rising = price_chg_5d[i] >= PRICE_UP_5D_MIN

            # 条件2: 10日趋势不弱
            trend_ok = price_chg_10d[i] is None or price_chg_10d[i] >= PRICE_UP_10D_MIN

            # 条件3: 收盘站上 MA10
            above_ma10 = close > ma10[i] if CLOSE_ABOVE_MA10 else True

            # 条件4: 量能放大（但不过度）
            vol_expanding = VOL_RATIO_BUY <= vol_ratio <= VOL_RATIO_MAX

            # 条件5: 量能加速（近3日 vs 前3日）
            vol_accelerating = True
            if i >= 6:
                recent_3_vol = sum(volumes[i - 2:i + 1]) / 3
                prior_3_vol = sum(volumes[i - 5:i - 2]) / 3
                if prior_3_vol > 0:
                    vol_accelerating = (recent_3_vol / prior_3_vol) >= VOL_TREND_ACCEL

            # 条件6: 量价配合（涨时放量 > 跌时缩量）
            dyn = _compute_price_volume_dynamics(closes, volumes, i)
            vol_healthy = dyn["up_vol_ratio"] >= UP_VOL_RATIO

            # 条件7: 不过度追高
            not_extended = close <= ma10[i] * PRICE_ABOVE_MA10_MAX

            # 条件8: 至少有连续上涨
            has_momentum = dyn["consecutive_up"] >= MIN_CONSEC_UP

            if (price_rising and trend_ok and above_ma10 and vol_expanding
                    and vol_accelerating and vol_healthy and not_extended and has_momentum):
                in_pos = True
                entry_price = close
                entry_index = i

                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": (
                        f"价增量增(5日涨{price_chg_5d[i]*100:.1f}%, "
                        f"量比{vol_ratio:.1f}x, "
                        f"连涨{dyn['consecutive_up']}天, "
                        f"涨跌量比{dyn['up_vol_ratio']:.1f}x)"
                    ),
                })
            continue

        # ==================== 卖出逻辑 ====================
        # v2 style: 硬止损 + 止盈 + 单日暴跌 + 持仓到期
        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        reason = None

        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = f"止盈({profit_pct:.1f}%)"
        elif daily_chg <= DAILY_CRASH_PCT / 100:
            reason = f"单日暴跌({daily_chg:.1%})"
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
