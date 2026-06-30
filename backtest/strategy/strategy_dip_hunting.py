# -*- coding: utf-8 -*-
"""
大跌抄底 V2 — 个股短期急跌后确认真反弹才抄底，博取快速反弹

V2 相比 V1 的核心改进（基于回测反馈）：
  V1 问题：77.5% 亏损来自"跌破低点"——买入假底（死猫反弹），胜率仅34%
  V2 改进：
    1. 2日确认：信号日下影线 + 次日收阳才买入（等反弹确认，不等第一根下影线）
    2. 量能衰竭：今日量 < 昨日量（卖压持续减弱，而非仍在放量恐慌中）
    3. RSI超卖：RSI(14) < 35（极度超卖区域）
    4. 跌速减缓：今日跌幅 < 昨日跌幅（不再加速下跌，处于筑底过程）
    5. 低点缓冲：跌破低点×0.97才止损（给底部测试留空间）
    6. 市场门控收紧：广度<30%禁止买入（熊市不抄底）

核心理念：
  个股短期急跌 → 恐慌盘大量涌出（放量） → 量能开始萎缩（卖压衰竭）
  → 下影线出现（抄底资金进场） → 次日收阳（真反弹确认）
  → 抄底买入，博 5~15 天反弹

使用方法：
  python script/run_strategy_market_backtest.py --strategy dip_hunting --max-positions 5
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma, rolling_high, rolling_low, rsi

META = {
    "id": "dip_hunting",
    "name": "大跌抄底 V2",
    "description": "V2: 急跌(5日<-10%)+RSI超卖+量能萎缩+下影线+次日收阳确认反弹。博12%止盈/-8%止损/15天到期。市场广度<30%禁买。",
}

# ============ 可调参数 ============

# -- 大跌检测 --
MAX_DECLINE_5D = -0.10          # 5日跌幅 < -10%（短期急跌确认）
MAX_DECLINE_FROM_HIGH = -0.15   # 距20日高点跌幅 > 15%（跌出性价比）
HIGH_LOOKBACK = 20              # 高点回看窗口

# -- 恐慌放量 + 量能衰竭 --
VOL_MA_PERIOD = 20              # 量能均线周期
VOL_PANIC_RATIO = 1.3           # 近3日均量 > VOL_MA × 此值 = 恐慌盘涌出过
VOL_DECLINE_RATIO = 0.85        # 今日量 < 昨日量 × 此值 = 量能萎缩（卖压衰竭）

# -- RSI 超卖 --
RSI_PERIOD = 14
RSI_OVERSOLD_MAX = 35           # RSI < 35 才考虑（极度超卖）

# -- 跌速减缓 --
MAX_DAILY_DROP = -0.03          # 今日跌幅不超过 -3%（不再加速）

# -- 下影线确认 --
LOWER_SHADOW_RATIO = 1.5        # 下影线 ≥ 实体 × 此值

# -- 反弹确认（2日模式）--
REBOUND_MIN_CHG = 0.005         # 次日涨幅 > 0.5%（真反弹，不是横盘）

# -- 流动性 --
MIN_AMOUNT_MA20 = 5_000_0000    # 20日均成交额 > 5000万（排除僵尸股）

# -- 卖出 --
STOP_LOSS_PCT = -8              # 硬止损（%）
TAKE_PROFIT_PCT = 12            # 止盈（%）
TRAILING_ACTIVE_PCT = 6         # 盈利 > 6% 才启动移动止盈
TRAILING_STOP_PCT = 5           # 从高点回落 -5% 卖出
MAX_HOLD_DAYS = 15              # 最大持仓天数
DIP_LOW_BUFFER = 0.97           # 跌破低点 × 0.97 才止损（3%缓冲）

# -- 一字跌停过滤 --
LIMIT_DOWN_PCT = -9.5           # 跌停阈值

# -- 市场门控 --
MARKET_FEAR_BREADTH = 0.30      # 广度 < 30%（熊市环境，不抄底）


# ============ 大盘门控（portfolio层调用） ============

def market_gate(date, market_stats):
    """市场门控：广度<30%禁止（熊市抄底胜率极低），其余允许。"""
    s = market_stats.get(date, {})
    breadth = s.get("breadth", 0.5)
    limit_down = s.get("limit_down", 0)

    if breadth < MARKET_FEAR_BREADTH:
        return {
            "allowed": False,
            "state": "BEAR",
            "reason": f"熊市环境(广度{breadth*100:.1f}%<{MARKET_FEAR_BREADTH*100:.0f}%)，不抄底",
        }

    # 极端踩踏（跌停>50只 且 无涨停）
    limit_up = s.get("limit_up", 0)
    if limit_up == 0 and limit_down >= 30:
        return {
            "allowed": False,
            "state": "PANIC",
            "reason": f"踩踏(跌停{limit_down}只，无涨停)",
        }

    return {"allowed": True, "state": "NORMAL", "reason": f"正常(广度{breadth*100:.1f}%)"}


# ============ 辅助函数 ============

def _calc_lower_shadow_ratio(open_p, high_p, low_p, close_p):
    """计算下影线与实体之比。"""
    body = abs(close_p - open_p)
    lower_shadow = min(open_p, close_p) - low_p
    if body < 0.001:
        return 999.0 if lower_shadow > 0.01 else 0.0
    return lower_shadow / body


# ============ 主策略 ============

def generate_signals(bars, market_stats=None):
    """
    生成买卖信号。

    买入（V2 两步确认）:
      第1天（信号日）：满足大跌+放量+下影线+RSI超卖+跌速减缓
      第2天（确认日）：收阳（涨>0.5%）→ 确认反弹有效，第2天收盘买入

    卖出: 止损-8% / 止盈+12% / 移动止盈 / 跌破低点(×0.97缓冲) / 15天到期

    参数:
        bars: 单只股票的K线列表
        market_stats: {date: {breadth, ...}} 市场日统计（可选）
    返回:
        list[dict]: 买卖信号列表
    """
    closes = [b["close"] for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    amounts = [b.get("amount") or 0 for b in bars]
    n = len(closes)

    # 预计算指标
    high_20 = rolling_high(highs, HIGH_LOOKBACK)
    low_5 = rolling_low(lows, 5)
    vol_ma20 = sma(volumes, VOL_MA_PERIOD)
    amount_ma20 = sma(amounts, VOL_MA_PERIOD)
    rsi14 = rsi(closes, RSI_PERIOD)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    entry_dip_low = None       # 大跌最低点 × 缓冲系数（用于跌破止损判断）
    peak_close = None
    pending_signal = None      # 第1天信号日的快照（等待第2天确认）

    min_idx = max(HIGH_LOOKBACK, VOL_MA_PERIOD, RSI_PERIOD) + 5

    for i in range(min_idx, n):
        close = closes[i]
        open_p = opens[i]
        high_p = highs[i]
        low_p = lows[i]
        date = bars[i]["trade_date"]
        volume = volumes[i]

        if high_20[i] is None or vol_ma20[i] is None or amount_ma20[i] is None:
            continue
        if rsi14[i] is None:
            continue
        if closes[i - 5] <= 0 or high_20[i] <= 0:
            continue

        daily_chg = (close / closes[i - 1] - 1) if i > 0 and closes[i - 1] > 0 else 0

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # --- 检查是否有待确认的信号（第2天确认逻辑） ---
            if pending_signal is not None:
                prev_info = pending_signal
                pending_signal = None  # 只等一天

                # 确认条件：次日收阳（收盘 > 开盘 且 涨 > 0.5%）
                if daily_chg >= REBOUND_MIN_CHG and close > open_p:
                    # 确认反弹有效，买入！

                    # 次日一字涨停无法成交
                    if daily_chg >= 0.095 and high_p == low_p:
                        continue

                    # 大跌最低点（5日最低）× 缓冲系数
                    dip_low = prev_info["dip_low"]
                    dip_low_buffered = dip_low * DIP_LOW_BUFFER

                    in_pos = True
                    entry_price = close
                    entry_index = i
                    entry_dip_low = dip_low_buffered
                    peak_close = close

                    info = prev_info
                    signals.append({
                        "date": date,
                        "action": "buy",
                        "reason": (
                            f"大跌抄底(5日跌{info['chg_5d']*100:.1f}%|"
                            f"距高{-info['decline_from_high']*100:.1f}%|"
                            f"量{info['vol_ratio']:.1f}x→缩{info['vol_decline_ratio']:.1f}|"
                            f"RSI{info['rsi']:.0f}|"
                            f"影{info['shadow_ratio']:.1f}x|"
                            f"确认涨{daily_chg*100:.1f}%|"
                            f"低点{dip_low:.2f})"
                        ),
                    })
                continue

            # --- 第1天：检测大跌 + 企稳信号 ---
            # 条件 1：短期急跌（5日跌幅 < -10%）
            chg_5d = (close - closes[i - 5]) / closes[i - 5]
            if chg_5d > MAX_DECLINE_5D:
                continue

            # 条件 2：高位回落（距20日高点 > 15%）
            decline_from_high = (close - high_20[i]) / high_20[i]
            if decline_from_high > MAX_DECLINE_FROM_HIGH:
                continue

            # 条件 3：RSI 极度超卖
            if rsi14[i] >= RSI_OVERSOLD_MAX:
                continue

            # 条件 4：恐慌放量（近3日均量 > 20日均量 × 1.3）— 确认恐慌盘出清过
            if i < 3:
                continue
            vol_3d_avg = sum(volumes[i - 2:i + 1]) / 3
            vol_ratio = vol_3d_avg / vol_ma20[i] if vol_ma20[i] > 0 else 0
            if vol_ratio < VOL_PANIC_RATIO:
                continue

            # 条件 5：量能萎缩（今日量 < 昨日量）— 卖压正在衰竭
            prev_volume = volumes[i - 1] if i > 0 else volume
            vol_decline_ratio = volume / prev_volume if prev_volume > 0 else 1.0
            if vol_decline_ratio > VOL_DECLINE_RATIO or volume >= prev_volume:
                continue

            # 条件 6：跌速减缓（今日跌幅 > -3%，不再加速下跌）
            if daily_chg <= MAX_DAILY_DROP:
                continue

            # 条件 7：跌速减缓（今日跌幅 < 昨日跌幅，处于减速过程）
            if i >= 2 and closes[i - 2] > 0:
                prev_daily_chg = (closes[i - 1] / closes[i - 2] - 1)
                if daily_chg < prev_daily_chg:  # 仍在加速下跌
                    continue

            # 条件 8：下影线确认（下影线 ≥ 实体 × 1.5）
            shadow_ratio = _calc_lower_shadow_ratio(open_p, high_p, low_p, close)
            if shadow_ratio < LOWER_SHADOW_RATIO:
                continue

            # 条件 9：非一字跌停
            if daily_chg * 100 <= LIMIT_DOWN_PCT:
                continue

            # 条件 10：流动性
            if amount_ma20[i] < MIN_AMOUNT_MA20:
                continue

            # ==== 第1天信号确认，等待第2天反弹确认 ====
            dip_low = low_5[i] if low_5[i] is not None else min(lows[i - 4:i + 1])

            pending_signal = {
                "date": date,
                "chg_5d": chg_5d,
                "decline_from_high": decline_from_high,
                "vol_ratio": vol_ratio,
                "vol_decline_ratio": vol_decline_ratio,
                "rsi": rsi14[i],
                "shadow_ratio": shadow_ratio,
                "dip_low": dip_low,
            }
            continue

        # ==================== 卖出逻辑 ====================
        if i == entry_index:
            continue

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        if close > peak_close:
            peak_close = close

        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close and peak_close > 0 else 0

        reason = None

        # 1. 硬止损
        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"

        # 2. 止盈
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = f"止盈({profit_pct:.1f}%)"

        # 3. 移动止盈（盈利>6%后启动，从高点回落-5%卖出）
        elif (profit_pct >= TRAILING_ACTIVE_PCT and
              peak_close > entry_price and
              drawdown_from_peak <= -TRAILING_STOP_PCT):
            reason = f"移动止盈(高{peak_close:.2f}回{drawdown_from_peak:.1f}%)"

        # 4. 抄底失败：跌破大跌最低点（含3%缓冲）
        elif entry_dip_low is not None and close < entry_dip_low:
            reason = f"抄底失败(跌破低点缓冲{entry_dip_low:.2f})"

        # 5. 持仓到期
        elif hold_days >= MAX_HOLD_DAYS:
            reason = f"持仓{hold_days}天到期(盈{profit_pct:.1f}%)"

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
        entry_dip_low = None
        peak_close = None

    return signals


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
