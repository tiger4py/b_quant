# -*- coding: utf-8 -*-
"""
大底抄底策略 — 只在市场恐慌底部买入超跌股，中长期持有等风来

核心理念：
  当全市场广度 < 25%（恐慌底），买入被错杀的超跌股
  （跌到MA60下方但卖压已衰竭），持有 60-90 天等市场修复反弹。

与"趋势跟随(动量追涨)"互补：
  - 动量追涨：每天都在追强势股，14天快进快出
  - 大底抄底：只在市场暴跌后出手，买超跌等反弹

策略流程：
  买入 = 市场恐慌(广度<25%) + 深度超跌(MA60下方>15%) + 卖压衰竭(5日-5%~0%)
  卖出 = 止损-12% / 移动止盈(从高点回落-15%) / 持仓90天到期
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma, atr

META = {
    "id": "market_bottom",
    "name": "大底抄底",
    "description": "市场恐慌底抄底：广度<25%时买入超跌股(MA60下方>8%+卖压衰竭5日-8%~-1%)。持有90天等反弹。卖出：ATR动态止损2.5x/移动止盈-15%(盈>10%启动)/90天到期。",
}

# ============ 可调参数 ============

# -- 市场恐慌门槛 --
MARKET_FEAR_BREADTH = 0.25       # 广度 < 此值 = 恐慌底，可以买入

# -- 选股条件 --
MA_TREND_PERIOD = 60             # 趋势判断：MA60 为中长期价值锚
MAX_BELOW_MA_PCT = -0.08         # 股价低于MA60至少8%（超跌确认）
MAX_DECLINE_5D = -0.08           # 5日跌幅不超过-8%（卖压衰竭）
MIN_DECLINE_5D = -0.01           # 5日跌幅不低于-1%（排除已反弹的，买还在跌但快止住的）

# -- 卖出 --
ATR_PERIOD = 20               # ATR 计算周期
ATR_STOP_MULT = 2.5            # 止损倍率：2.5x ATR
ATR_STOP_MIN = 8               # 最低止损幅度%（低波动股不低于此值）
ATR_STOP_MAX = 22              # 最高止损幅度%（高波动股不超此值，控制风险）
TRAILING_STOP_PCT = 15         # 移动止盈回落幅度（%）
TRAILING_ACTIVE_PCT = 10       # 盈利 > 10% 才启动移动止盈
MAX_HOLD_DAYS = 90             # 最大持仓天数（约4个月）


# ============ 大盘门控（portfolio层调用） ============

def market_gate(date, market_stats):
    """portfolio 层调用：恐慌底允许买入，其余不允许（只在恐慌出手）。"""
    s = market_stats.get(date, {})
    breadth = s.get("breadth", 0.5)

    if breadth < MARKET_FEAR_BREADTH:
        return {"allowed": True, "state": "FEAR",
                "reason": f"恐慌底(广度{breadth:.2f})"}
    else:
        return {"allowed": False, "state": "NORMAL",
                "reason": f"非恐慌(广度{breadth:.2f})"}


# ============ 主策略 ============

def generate_signals(bars, market_stats=None):
    """
    买入: 市场恐慌 + 深度超跌（MA60下方>8%）+ 卖压衰竭（5日-8%~-1%）
    卖出: ATR动态止损 / 移动止盈 / 90天到期

    参数:
        bars: 单只股票的K线列表
        market_stats: {date: {breadth, ...}} 市场日统计（可选，用于判断恐慌）
    返回:
        list[dict]: 买卖信号列表
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(closes)

    ma_trend = sma(closes, MA_TREND_PERIOD)
    atr_values = atr(bars, ATR_PERIOD)  # ATR动态止损

    # 预计算所有日期的广度
    breadth_by_date = {}
    if market_stats is not None:
        breadth_by_date = {d: s.get("breadth", 0.5) for d, s in market_stats.items()}

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    peak_close = None
    entry_stop_pct = None  # ATR动态止损百分比

    min_idx = max(MA_TREND_PERIOD, ATR_PERIOD) + 10  # MA60/ATR + buffer

    for i in range(min_idx, n):
        close = closes[i]
        date = bars[i]["trade_date"]

        if ma_trend[i] is None or atr_values[i] is None:
            continue

        daily_chg = (close - closes[i - 1]) / closes[i - 1] if i > 0 and closes[i - 1] > 0 else 0

        # 获取当日市场广度
        breadth = breadth_by_date.get(date, 0.5)

        # ==================== 买入 ====================
        if not in_pos:
            # 只在恐慌期买入
            if breadth >= MARKET_FEAR_BREADTH:
                continue

            # 深度超跌：股价在MA60下方至少8%
            if ma_trend[i] <= 0:
                continue
            below_ma_pct = (close / ma_trend[i] - 1)
            if below_ma_pct > MAX_BELOW_MA_PCT:
                continue

            # 卖压衰竭：5日跌幅在 [-8%, -1%] 区间
            if closes[i - 5] <= 0:
                continue
            chg_5d = (close - closes[i - 5]) / closes[i - 5]
            if chg_5d < MAX_DECLINE_5D or chg_5d > MIN_DECLINE_5D:
                continue

            # 非ST、非一字跌停
            if daily_chg <= -0.095:
                continue

            # ==== 买入（次日收盘价成交）====
            if i + 1 >= n:
                continue

            next_high = highs[i + 1]
            next_low = lows[i + 1]
            next_close = closes[i + 1]
            next_chg = (next_close - close) / close if close > 0 else 0
            if next_high == next_low and next_chg >= 0.095:
                continue

            # ATR动态止损：高波动股给更多空间
            atr_pct = atr_values[i] / close * 100  # ATR占价格的百分比
            stop_pct = -max(ATR_STOP_MIN, min(ATR_STOP_MAX, ATR_STOP_MULT * atr_pct))

            in_pos = True
            entry_price = next_close
            entry_index = i + 1
            peak_close = next_close
            entry_stop_pct = stop_pct

            signals.append({
                "date": bars[i + 1]["trade_date"],
                "action": "buy",
                "reason": (
                    f"大底抄底(广度{breadth*100:.0f}%恐慌 | "
                    f"低于MA{MA_TREND_PERIOD}{below_ma_pct*100:.1f}%超跌 | "
                    f"5日{chg_5d*100:.1f}% | "
                    f"ATR{atr_pct:.1f}%止损{stop_pct:.0f}%)"
                ),
            })
            continue

        # ==================== 卖出 ====================
        if i == entry_index:
            continue

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        if close > peak_close:
            peak_close = close

        drawdown_from_peak = (close / peak_close - 1) * 100 if peak_close and peak_close > 0 else 0

        reason = None

        # 1. ATR动态止损（每只股票止损幅度不同，根据买入时的ATR决定）
        if entry_stop_pct is not None and profit_pct <= entry_stop_pct:
            reason = f"ATR止损({profit_pct:.1f}%,ATR限{entry_stop_pct:.0f}%)"

        # 2. 移动止盈（盈利>10%后启动，回落-15%卖出）
        elif (profit_pct >= TRAILING_ACTIVE_PCT and
              peak_close > entry_price and
              drawdown_from_peak <= -TRAILING_STOP_PCT):
            reason = f"移动止盈(高{peak_close:.2f}回{drawdown_from_peak:.1f}%)"

        # 3. 持仓到期
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
        peak_close = None
        entry_stop_pct = None

    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta
    run_strategy_meta(META)
