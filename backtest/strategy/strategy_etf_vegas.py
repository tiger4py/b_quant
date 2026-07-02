# -*- coding: utf-8 -*-
"""
ETF 维加斯隧道策略 — 类 Vegas Tunnel 风格，针对 ETF 特性优化

核心理念：
  ETF 代表一篮子资产，趋势性比个股更强、波动更平滑，适合均线隧道系统。
  原版 Vegas Tunnel 使用 EMA12/EMA144/EMA169/EMA576 四个周期，
  ETF 版使用更短的 EMA 周期（EMA12/EMA50/EMA100/EMA200），
  因为 ETF 的趋势转换更快，不需要超长周期过滤。

隧道系统：
  - EMA50 和 EMA100 构成"隧道"（趋势过滤器），两者之间的区域为支撑/阻力带
  - EMA12 为信号线（快线），用于判断进出场时机
  - EMA12 上穿隧道 → 趋势转多 → 买入
  - EMA12 下穿隧道 → 趋势转空 → 卖出
  - EMA200 为长期趋势过滤器，仅在价格 > EMA200 时允许买入

经典来源：Vegas (The New Laws of the World Wide Web, 2001)
ETF 适配：缩短均线周期 + 放宽隧道宽度 + 增加 RSI 辅助过滤

用法示例:
  python script/run_etf_backtest.py --strategy etf_vegas
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import ema, sma, rsi

META = {
    "id": "etf_vegas",
    "name": "ETF维加斯隧道",
    "description": (
        "ETF版维加斯隧道：EMA12/EMA50/EMA100/EMA200四线隧道系统。"
        "买入=EMA12上穿隧道+隧道多头排列+EMA200趋势过滤+RSI辅助+不追高。"
        "卖出=EMA12下穿隧道/隧道倒置/高位回撤-10%/持仓50天。适配ETF强趋势特性。"
    ),
}

# ============ 可调参数 ============

# -- 均线周期 --
EMA_FAST = 12       # 信号线（快线），用于触发买卖
EMA_TUNNEL_LO = 50  # 隧道下沿（慢线 1）
EMA_TUNNEL_HI = 100 # 隧道上沿（慢线 2）
EMA_LONG = 200      # 长期趋势过滤（0=禁用），仅 close > EMA_LONG 允许买入

# -- RSI 辅助过滤 --
RSI_WINDOW = 14
RSI_BUY_MIN = 30    # RSI 不低于 30（不在恐慌中接刀）
RSI_BUY_MAX = 80    # RSI 不高于 80（不追极端超买）

# -- 买入过滤 --
VOL_RATIO_MIN = 0.70     # vol_5d / vol_20d 最小比值（0.7=允许小幅缩量）
VOL_RATIO_MAX = 5.0      # vol_5d / vol_20d 最大比值（排除异常爆量）
MAX_EXTENSION = 0.12     # 收盘价距隧道上沿最大距离%（防追高，12%）

# -- 流动性 --
MIN_AMOUNT = 1_000_000   # ETF 最低日成交额（100万）

# -- 卖出 --
STOP_LOSS_PCT = -99      # 硬止损（-99=禁用）
TAKE_PROFIT_PCT = 999    # 止盈（999=禁用）
HIGH_RETREAT_PCT = -10   # 高位回撤：距持仓期间最高点回撤超此值离场（%）
MAX_HOLD_DAYS = 50       # 最大持仓天数
TUNNEL_COLLAPSE_EXIT = True  # 隧道倒置时离场（EMA50 < EMA100）


# ============ 辅助函数 ============

def _cross_above(fast_today, fast_yesterday, threshold):
    """判断快线是否上穿阈值：今天在上、昨天在下或等于。"""
    return fast_today > threshold and fast_yesterday <= threshold


# ============ 主策略 ============

def generate_signals(bars):
    """生成 ETF 买卖信号。

    买入 = EMA12 上穿隧道 + 隧道多头排列 + EMA200 趋势过滤 + RSI 辅助 + 不追高
    卖出 = EMA12 下穿隧道 / 隧道倒置 / 高位回撤-10% / 持仓 50 天

    参数:
        bars: K 线列表，每项为 dict，含 open/high/low/close/volume/trade_date

    返回:
        list[dict]: 信号列表，每项含 date/action/reason
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    amounts = [b.get("amount") or 0 for b in bars]
    n = len(closes)

    # ---- 预计算指标 ----
    ema12 = ema(closes, EMA_FAST)
    ema50 = ema(closes, EMA_TUNNEL_LO)
    ema100 = ema(closes, EMA_TUNNEL_HI)
    ema200 = ema(closes, EMA_LONG) if EMA_LONG > 0 else None
    rsi14 = rsi(closes, RSI_WINDOW)

    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    peak_close = None      # 持仓期间最高收盘价（高位回撤用）

    # 最小索引：需要足够数据计算最长的 EMA
    max_period = max(EMA_TUNNEL_HI, EMA_LONG)
    min_idx = max_period + 20

    for i in range(min_idx, n):
        close = closes[i]
        amount = amounts[i]

        # ---- 检查指标有效性 ----
        if ema12[i] is None or ema50[i] is None or ema100[i] is None:
            continue
        if rsi14[i] is None:
            continue
        if EMA_LONG > 0 and (ema200 is None or ema200[i] is None):
            continue
        if vol_ma5[i] is None or vol_ma20[i] is None or vol_ma20[i] == 0:
            continue

        vol_ratio = vol_ma5[i] / vol_ma20[i]
        curr_rsi = rsi14[i]

        # 隧道上下沿
        tunnel_upper = max(ema50[i], ema100[i])
        tunnel_lower = min(ema50[i], ema100[i])

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # ---- 条件 1: EMA12 上穿隧道 ----
            crossed_50 = _cross_above(ema12[i], ema12[i - 1], ema50[i - 1])
            crossed_100 = _cross_above(ema12[i], ema12[i - 1], ema100[i - 1])
            above_both_today = ema12[i] > ema50[i] and ema12[i] > ema100[i]
            below_one_yesterday = ema12[i - 1] <= ema50[i - 1] or ema12[i - 1] <= ema100[i - 1]
            crossed_today = crossed_50 or crossed_100 or (above_both_today and below_one_yesterday)

            if not crossed_today:
                continue

            # ---- 条件 2: 隧道多头排列 ----
            if ema50[i] <= ema100[i]:
                continue

            # ---- 条件 3: EMA200 长期趋势过滤 ----
            if EMA_LONG > 0 and ema200 is not None:
                if close <= ema200[i]:
                    continue

            # ---- 条件 4: RSI 辅助过滤 ----
            if curr_rsi < RSI_BUY_MIN or curr_rsi > RSI_BUY_MAX:
                continue

            # ---- 条件 5: 量能趋势确认 ----
            if vol_ratio < VOL_RATIO_MIN or vol_ratio > VOL_RATIO_MAX:
                continue

            # ---- 条件 6: 不追高 ----
            extension = (close - tunnel_upper) / tunnel_upper if tunnel_upper > 0 else 0
            if extension > MAX_EXTENSION:
                continue

            # ---- 条件 7: 流动性 ----
            if amount < MIN_AMOUNT:
                continue

            # ==== 全部条件满足 → 买入（次日成交） ====
            if i + 1 >= n:
                continue

            next_high = highs[i + 1]
            next_low = lows[i + 1]
            next_close = closes[i + 1]
            next_chg = (next_close - close) / close if close > 0 else 0
            # ETF 虽然不会涨停，但保留一字板过滤（极端行情）
            if next_high == next_low and abs(next_chg) >= 0.095:
                continue

            # 入场日再确认（次日 EMA12 仍在隧道上方）
            if ema12[i + 1] is None or ema50[i + 1] is None or ema100[i + 1] is None:
                continue
            if ema12[i + 1] <= max(ema50[i + 1], ema100[i + 1]):
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
                    f"ETF Vegas(EMA12上穿隧道 "
                    f"隧道宽{t_width:.1f}% | 距隧道{extension*100:.1f}% | "
                    f"RSI{curr_rsi:.0f} | 量比{vol_ratio:.1f})"
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

        # ---- 优先级 1: 硬止损（禁用） ----
        if profit_pct <= STOP_LOSS_PCT:
            reason = "止损(%.1f%%)" % profit_pct

        # ---- 优先级 2: EMA12 下穿隧道 ----
        elif (
            ema12[i] < ema50[i] or ema12[i] < ema100[i]
        ) and (
            ema12[i - 1] >= ema50[i - 1] and ema12[i - 1] >= ema100[i - 1]
        ):
            reason = "EMA12下穿隧道(盈%.1f%%)" % profit_pct

        # ---- 优先级 3: 隧道倒置 ----
        elif TUNNEL_COLLAPSE_EXIT and ema50[i] <= ema100[i]:
            reason = "隧道倒置(EMA50<EMA100,盈%.1f%%)" % profit_pct

        # ---- 优先级 4: 止盈（禁用） ----
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = "止盈(%.1f%%)" % profit_pct

        # ---- 优先级 5: 高位回撤 ----
        elif retreat_pct <= HIGH_RETREAT_PCT:
            reason = "高位回撤(%.1f%%,峰值%.3f)" % (retreat_pct, peak_close)

        # ---- 优先级 6: 持仓到期 ----
        elif hold_days >= MAX_HOLD_DAYS:
            reason = "持仓%d天到期(盈%.1f%%)" % (hold_days, profit_pct)

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
