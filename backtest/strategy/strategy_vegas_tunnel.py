# -*- coding: utf-8 -*-
"""
维加斯隧道策略 (Vegas Tunnel Strategy) — 增强版

核心理念：利用 EMA12 / EMA144 / EMA169 三条均线构成"隧道"系统。
  - EMA144 和 EMA169 构成"隧道"（趋势过滤器），两者之间的区域为支撑/阻力带
  - EMA12 为信号线（快线），用于判断进出场时机
  - EMA12 上穿隧道 → 趋势转多 → 买入
  - EMA12 下穿隧道 → 趋势转空 → 卖出

经典来源：Vegas (The New Laws of the World Wide Web, 2001)
A股适配：仅做多，增加追高防护和多重卖出保护（高位回撤+到期+隧道倒置），无固定止损止盈。

用法示例:
  python backtest/strategy/strategy_vegas_tunnel.py                          # 默认参数回测
  python script/run_strategy_market_backtest.py --strategy vegas_tunnel      # 全市场回测
"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import ema, sma

META = {
    "id": "vegas_tunnel",
    "name": "维加斯隧道",
    "description": (
        "维加斯隧道(增强版)：EMA12/EMA144/EMA169三线隧道系统。"
        "买入=EMA12上穿隧道+隧道多头排列+EMA576趋势过滤+不追高。"
        "卖出=EMA12下穿隧道/隧道倒置/高位回撤-12%/持仓40天。"
        "无固定止损止盈，依靠多重退出机制。"
    ),
}

# ============ 可调参数 ============

# -- 均线周期 --
EMA_FAST = 12           # 信号线（快线），用于触发买卖
EMA_TUNNEL_LO = 144     # 隧道下沿（慢线1）
EMA_TUNNEL_HI = 169     # 隧道上沿（慢线2）
EMA_LONG = 576          # 长期趋势过滤（0=禁用），仅在价格>EMA_LONG时允许买入

# -- 买入过滤 --
VOL_RATIO_MIN = 1.0     # vol_5d / vol_20d 最小比值（1.0=至少不缩量）
VOL_RATIO_MAX = 5.0     # vol_5d / vol_20d 最大比值（排除异常爆量）
MAX_EXTENSION = 0.12    # 收盘价距隧道上沿最大距离%（防追高，12%=突破后最多涨12%）

# -- 卖出 --
STOP_LOSS_PCT = -99         # 硬止损（-99=禁用）
TAKE_PROFIT_PCT = 999       # 止盈（999=禁用）
HIGH_RETREAT_PCT = -12      # 高位回撤：距持仓期间最高点回撤超此值离场（%）
MAX_HOLD_DAYS = 40          # 最大持仓天数
TUNNEL_COLLAPSE_EXIT = True # 隧道倒置时离场（EMA144 < EMA169 时反转）


# ============ 辅助函数 ============

def _cross_above(fast_today, fast_yesterday, threshold):
    """判断快线是否上穿阈值：今天在上、昨天在下或等于"""
    return fast_today > threshold and fast_yesterday <= threshold


# ============ 主策略 ============

def generate_signals(bars):
    """
    生成买卖信号。

    买入 = EMA12上穿隧道 + 隧道多头排列 + EMA576趋势过滤 + 不追高
    卖出 = EMA12下穿隧道（唯一规则）

    参数:
        bars: K线列表，每项为 dict，含 open/high/low/close/volume/trade_date

    返回:
        list[dict]: 信号列表，每项含 date/action/reason
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(closes)

    # ---- 预计算指标 ----
    ema12 = ema(closes, EMA_FAST)
    ema144 = ema(closes, EMA_TUNNEL_LO)
    ema169 = ema(closes, EMA_TUNNEL_HI)
    ema576 = ema(closes, EMA_LONG) if EMA_LONG > 0 else None

    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    peak_close = None      # 持仓期间最高收盘价（高位回撤用）

    # 最小索引：需要足够数据计算最长的EMA
    max_period = max(EMA_TUNNEL_HI, EMA_LONG)
    min_idx = max_period + 20

    for i in range(min_idx, n):
        close = closes[i]

        # ---- 检查指标有效性 ----
        if ema12[i] is None or ema144[i] is None or ema169[i] is None:
            continue
        if EMA_LONG > 0 and (ema576 is None or ema576[i] is None):
            continue
        if vol_ma5[i] is None or vol_ma20[i] is None or vol_ma20[i] == 0:
            continue

        vol_ratio = vol_ma5[i] / vol_ma20[i]

        # 隧道上下沿
        tunnel_upper = max(ema144[i], ema169[i])
        tunnel_lower = min(ema144[i], ema169[i])

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # ---- 条件1: EMA12 上穿隧道 ----
            crossed_144 = _cross_above(ema12[i], ema12[i - 1], ema144[i - 1])
            crossed_169 = _cross_above(ema12[i], ema12[i - 1], ema169[i - 1])
            above_both_today = ema12[i] > ema144[i] and ema12[i] > ema169[i]
            below_one_yesterday = ema12[i - 1] <= ema144[i - 1] or ema12[i - 1] <= ema169[i - 1]
            crossed_today = crossed_144 or crossed_169 or (above_both_today and below_one_yesterday)

            if not crossed_today:
                continue

            # ---- 条件2: 隧道多头排列 ----
            if ema144[i] <= ema169[i]:
                continue

            # ---- 条件3: EMA576 长期趋势过滤 ----
            if EMA_LONG > 0 and ema576 is not None:
                if close <= ema576[i]:
                    continue

            # ---- 条件4: 量能趋势确认 ----
            if vol_ratio < VOL_RATIO_MIN or vol_ratio > VOL_RATIO_MAX:
                continue

            # ---- 条件5: 不追高 ----
            extension = (close - tunnel_upper) / tunnel_upper if tunnel_upper > 0 else 0
            if extension > MAX_EXTENSION:
                continue

            # ==== 全部条件满足 → 买入（次日成交） ====
            if i + 1 >= n:
                continue

            next_high = highs[i + 1]
            next_low = lows[i + 1]
            next_close = closes[i + 1]
            next_chg = (next_close - close) / close if close > 0 else 0
            if next_high == next_low and next_chg >= 0.095:
                continue  # 一字涨停

            # 入场日再确认
            if ema12[i + 1] is None or ema144[i + 1] is None or ema169[i + 1] is None:
                continue
            if ema12[i + 1] <= max(ema144[i + 1], ema169[i + 1]):
                continue

            in_pos = True
            entry_price = next_close
            entry_index = i + 1
            peak_close = next_close

            t_width = (tunnel_upper - tunnel_lower) / tunnel_lower * 100 if tunnel_lower > 0 else 0

            signals.append({
                "date": bars[i + 1]["trade_date"],
                "action": "buy",
                "reason": (
                    "EMA12上穿隧道(隧道宽%.1f%% | 距隧道%.1f%% | 价格%.2f)"
                    % (t_width, extension * 100, next_close)
                ),
            })
            continue

        # ==================== 卖出逻辑 ====================
        if i == entry_index:
            if close > peak_close:
                peak_close = close
            continue

        # 更新持仓期间最高价
        if close > peak_close:
            peak_close = close

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        retreat_pct = (close / peak_close - 1) * 100 if peak_close else 0

        reason = None

        # ---- 优先级1: 硬止损（禁用） ----
        if profit_pct <= STOP_LOSS_PCT:
            reason = "止损(%.1f%%)" % profit_pct

        # ---- 优先级2: EMA12 下穿隧道 ----
        elif (
            ema12[i] < ema144[i] or ema12[i] < ema169[i]
        ) and (
            ema12[i - 1] >= ema144[i - 1] and ema12[i - 1] >= ema169[i - 1]
        ):
            reason = "EMA12下穿隧道(%.1f%%)" % profit_pct

        # ---- 优先级3: 隧道倒置 ----
        elif TUNNEL_COLLAPSE_EXIT and ema144[i] <= ema169[i]:
            reason = "隧道倒置(EMA144<EMA169, %.1f%%)" % profit_pct

        # ---- 优先级4: 止盈（禁用） ----
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = "止盈(%.1f%%)" % profit_pct

        # ---- 优先级5: 高位回撤 ----
        elif retreat_pct <= HIGH_RETREAT_PCT:
            reason = "高位回撤(%.1f%%, 峰值%.2f)" % (retreat_pct, peak_close)

        # ---- 优先级6: 持仓到期 ----
        elif hold_days >= MAX_HOLD_DAYS:
            reason = "持仓%d天到期(%.1f%%)" % (hold_days, profit_pct)

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

    return signals


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
