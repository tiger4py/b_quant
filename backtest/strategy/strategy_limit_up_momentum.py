# -*- coding: utf-8 -*-
"""
涨停惯性策略 (Limit-Up Momentum Strategy)

核心理念：涨停之后的股票更可能继续涨停（涨停板惯性效应）。
  1. 检测涨停事件：今日出现涨停（≥9.5%）→ 主力资金强力做多
  2. 次日追涨入场：次日继续走强（收阳 + 放量）→ 惯性确认，入场
  3. 归于平静卖出：波动率萎缩、量能回归正常、或价格跌破关键支撑 → 离场

关键指标：
- limit_up:     涨停检测（涨幅 ≥ 9.5%，排除一字板）
- post_lu_chg:  涨停次日涨跌幅 → 必须 > 0（继续走强才追）
- vol_surge:    涨停日量能 / 20日均量 → 放量确认（≥2.0x）
- atr_ratio:    当前ATR / 入场时ATR → 波动率是否消退
- vol_ratio:    当前量比 → 量能是否回归正常

策略流程：
- 涨停日检测 → 次日确认（收阳+量能持续）→ 入场
- 持有期间监控：波动率消退 / 量能回归 / 移动止盈 / 止损
"""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma, atr

META = {
    "id": "limit_up_momentum",
    "name": "涨停惯性",
    "description": "涨停次日确认入场：涨停日放量→次日收阳+量能不衰→入场追惯性。波动率/量能消退即「归于平静」卖出。捕捉涨停板短期动量延续。",
}

# ============ 可调参数 ============

# -- 涨停检测 --
LIMIT_UP_PCT = 9.5            # 涨停阈值（%），主板≥9.5%
LIMIT_UP_STREAK_MAX = 2       # 连续涨停超过N天不追（已经是第3个板以上 → 风险太高）

# -- 涨停日质量 --
VOL_SURGE_RATIO = 2.0         # 涨停日量能 / 20日均量 ≥ 此值 → 放量涨停有效
LU_CLOSE_NEAR_HIGH = 0.95     # 涨停日收盘价 / 最高价 ≥ 此值 → 封板坚决（不炸板）

# -- 入场确认（涨停次日）--
POST_LU_MIN_CHG = 0.0         # 涨停次日必须收阳（涨幅≥0%），不能高开低走
POST_LU_MAX_CHG = 9.0         # 涨停次日涨幅上限（%），如果次日又涨停 → 买不到/追太高
POST_LU_VOL_MIN = 1.2         # 涨停次日量 / 20日均量 ≥ 此值 → 量能持续活跃
POST_LU_NOT_CRASH = -3.0      # 涨停次日最低涨幅（兜底，如果开盘崩盘不买）

# -- 位置过滤 --
PRICE_ABOVE_LU_CLOSE = 0.97   # 当前价 / 涨停日收盘价 ≥ 此值 → 不能大幅跌破涨停价

# -- 卖出：归于平静 --
ATR_FADE_RATIO = 0.55         # 当前ATR / 入场时ATR ≤ 此值 → 波动消退，离场
VOL_FADE_RATIO = 0.9          # 当前5日均量 / 20日均量 ≤ 此值 → 量能回归正常，离场
PRICE_BELOW_LU_CLOSE = 0.97   # 收盘价跌破涨停日收盘价 * 此值 → 惯性消失

# -- 风控 --
STOP_LOSS_PCT = -7            # 硬止损（%），紧止损适配动量策略
TAKE_PROFIT_PCT = 15          # 止盈（%），短线快进快出
TRAILING_STOP_PCT = 5         # 移动止盈回撤（%），从持仓最高点回撤超过此值 → 止盈
MAX_HOLD_DAYS = 8             # 最大持仓天数
DAILY_CRASH_PCT = -6          # 单日暴跌离场（%）

# -- 大盘过滤 --
MIN_BREADTH = 0.35            # 市场涨跌比低于此值 → 恐慌，不追涨停
MIN_AMOUNT_RATIO = 0.80       # 今日成交额/20日均成交额低于此值 → 缩量市，不追


# ============ 涨停检测 ============

def _detect_limit_up_today(daily_change, i):
    """
    检测今天是否是涨停日。

    参数:
        daily_change: 每日涨跌幅序列（小数）
        i:            当前索引

    返回:
        (is_lu, lu_change_pct) 或 (False, 0)
    """
    if i < 1:
        return False, 0
    chg_pct = daily_change[i] * 100
    if chg_pct >= LIMIT_UP_PCT:
        return True, round(chg_pct, 2)
    return False, 0


def _count_consecutive_limit_ups(daily_change, lu_index):
    """
    计算涨停日之前连续涨停的天数（含当天）。
    如果涨停日之前已经连续涨停多天 → 过度拉升，不追。

    返回: 连续涨停天数
    """
    count = 1
    for j in range(lu_index - 1, max(0, lu_index - 10), -1):
        if daily_change[j] * 100 >= LIMIT_UP_PCT:
            count += 1
        else:
            break
    return count


def _is_one_word_limit_up(high, low, chg_pct):
    """检测是否一字涨停（全日最高=最低，且涨幅≥涨停阈值）→ 买不到"""
    return high == low and chg_pct >= LIMIT_UP_PCT


# ============ 主策略 ============

def generate_signals(bars):
    """
    生成买卖信号。

    买入 = 涨停日放量封板 + 次日收阳量能不衰 + 价格未崩盘
    卖出 = 波动消退 / 量能回归 / 惯性消失 / 移动止盈 / 止损
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(closes)

    # 预计算指标
    daily_change = [0.0]
    for i in range(1, n):
        chg = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0
        daily_change.append(chg)

    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)
    atr5 = atr(bars, 5)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    entry_atr5 = None           # 入场时的ATR，用于比较波动消退
    limit_up_close = None       # 涨停日收盘价，惯性锚点
    highest_since_entry = None  # 持仓期间最高收盘价，用于移动止盈

    min_idx = 42  # 20日均量需要20根 + buffer

    for i in range(min_idx, n):
        close = closes[i]
        high = highs[i]
        low = lows[i]

        # 检查指标有效性
        if vol_ma20[i] is None or vol_ma20[i] == 0:
            continue
        if atr5[i] is None:
            continue

        daily_chg = daily_change[i]
        vol_ratio_5_20 = vol_ma5[i] / vol_ma20[i] if vol_ma20[i] > 0 else 1.0
        today_vol_ratio = volumes[i] / vol_ma20[i] if vol_ma20[i] > 0 else 0

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # 条件 1：昨天是涨停日（今天来验证并买入）
            # i-1 是涨停日，i 是确认日
            if i < 1:
                continue
            is_lu, lu_chg_pct = _detect_limit_up_today(daily_change, i - 1)
            if not is_lu:
                continue

            lu_index = i - 1

            # 条件 2：连续涨停不超过阈值（不追第3个板以上）
            streak = _count_consecutive_limit_ups(daily_change, lu_index)
            if streak > LIMIT_UP_STREAK_MAX:
                continue

            # 条件 3：涨停日不是一字板（买不到）
            if _is_one_word_limit_up(highs[lu_index], lows[lu_index], lu_chg_pct):
                continue

            # 条件 4：涨停日封板坚决（收盘接近最高价 → 不炸板）
            lu_high = highs[lu_index]
            lu_close = closes[lu_index]
            if lu_high > 0 and lu_close / lu_high < LU_CLOSE_NEAR_HIGH:
                continue

            # 条件 5：涨停日放量
            if vol_ma20[lu_index] is None or vol_ma20[lu_index] == 0:
                continue
            lu_vol_ratio = volumes[lu_index] / vol_ma20[lu_index]
            if lu_vol_ratio < VOL_SURGE_RATIO:
                continue

            # 条件 6：涨停次日（今天）收阳，确认惯性延续
            if daily_chg <= POST_LU_MIN_CHG / 100:
                continue
            if daily_chg * 100 >= POST_LU_MAX_CHG:
                # 次日又涨停了 → 可能买不到（一字板）或已经太高
                if _is_one_word_limit_up(high, low, daily_chg * 100):
                    continue
                # 如果不是一字板，可以追，但价格不能太高
                if daily_chg * 100 >= 15:
                    continue

            # 条件 7：涨停次日量能保持活跃
            if today_vol_ratio < POST_LU_VOL_MIN:
                continue

            # 条件 8：涨停次日不能是从高位崩盘（开盘价不能比涨停日收盘价高太多然后暴跌）
            # 用最低价/涨停日收盘价来判断日内是否有崩盘
            post_lu_day_low_ratio = low / lu_close if lu_close > 0 else 1
            if post_lu_day_low_ratio < 0.95:
                # 日内有大幅下探（超过5%），不是好的追涨时机
                continue

            # 条件 9：当前价不能大幅跌破涨停日收盘价
            if lu_close > 0 and close / lu_close < PRICE_ABOVE_LU_CLOSE:
                continue

            # ==== 全部条件满足 → 买入 ====
            in_pos = True
            entry_price = close
            entry_index = i
            entry_atr5 = atr5[i]
            limit_up_close = lu_close
            highest_since_entry = close

            signals.append({
                "date": bars[i]["trade_date"],
                "action": "buy",
                "reason": (
                    f"涨停惯性({lu_chg_pct:.1f}%涨停|连板{streak}|"
                    f"量比{lu_vol_ratio:.1f}x|"
                    f"次日{daily_chg*100:+.1f}%|量{vol_ratio_5_20:.1f}x)"
                ),
            })
            continue

        # ==================== 卖出逻辑 ====================
        if i == entry_index:
            continue

        # 更新持仓期间最高价
        if close > highest_since_entry:
            highest_since_entry = close

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        peak_profit = (highest_since_entry / entry_price - 1) * 100 if entry_price else 0

        reason = None

        # ---- 优先级1：硬止损 ----
        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"

        # ---- 优先级2：单日暴跌 ----
        elif daily_chg <= DAILY_CRASH_PCT / 100:
            reason = f"单日暴跌({daily_chg*100:.1f}%)"

        # ---- 优先级3：归于平静 - 波动率消退 ----
        elif entry_atr5 and atr5[i] and entry_atr5 > 0:
            atr_ratio = atr5[i] / entry_atr5
            if atr_ratio <= ATR_FADE_RATIO and hold_days >= 2:
                reason = f"波动消退(ATR比{atr_ratio:.2f})"

        # ---- 优先级4：归于平静 - 量能回归 ----
        elif vol_ratio_5_20 <= VOL_FADE_RATIO and hold_days >= 2:
            reason = f"量能回归(量比{vol_ratio_5_20:.2f})"

        # ---- 优先级5：止盈 ----
        elif profit_pct >= TAKE_PROFIT_PCT:
            reason = f"止盈({profit_pct:.1f}%)"

        # ---- 优先级6：移动止盈（获利回撤保护）----
        elif peak_profit >= 8 and (peak_profit - profit_pct) >= TRAILING_STOP_PCT:
            reason = f"移动止盈(峰值{peak_profit:.1f}%→{profit_pct:.1f}%)"

        # ---- 优先级7：归于平静 - 惯性消失（跌破涨停日收盘价）----
        elif limit_up_close and close < limit_up_close * PRICE_BELOW_LU_CLOSE:
            reason = f"惯性消失(跌破涨停价{limit_up_close:.2f})"

        # ---- 优先级8：持仓到期 ----
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
        entry_atr5 = None
        limit_up_close = None
        highest_since_entry = None

    return signals


# ============ 大盘过滤 ============

def market_gate(date, market_stats):
    """
    大盘环境过滤：恐慌市/缩量市不追涨停。

    三重过滤：
    1. 广度过滤：涨跌比 < MIN_BREADTH → 恐慌
    2. 情绪过滤：跌停数 > 涨停数 + 阈值 → 恐慌
    3. 量能过滤：今日成交额 < 20日均额的 MIN_AMOUNT_RATIO → 缩量观望

    返回: {"allowed": bool, "reasons": [...]}
    """
    today = market_stats.get(date)
    if not today:
        return {"allowed": False, "reasons": ["无市场数据"]}

    reasons = []
    allowed = True

    breadth = today.get("breadth", 0.5)
    limit_balance = today.get("limit_balance", 0)
    amount = today.get("amount", 1e10)
    amount_ma20 = today.get("amount_ma20", amount)

    # 1. 涨跌比太低 → 恐慌
    if breadth < MIN_BREADTH:
        allowed = False
        reasons.append(f"市场广度不足({breadth:.1%}<{MIN_BREADTH:.0%})")

    # 2. 跌停潮 → 情绪恐慌
    if limit_balance < -15:
        allowed = False
        reasons.append(f"跌停潮(净跌停{limit_balance}只)")

    # 3. 缩量市 → 资金观望，涨停惯性弱
    if amount_ma20 > 0 and amount / amount_ma20 < MIN_AMOUNT_RATIO:
        allowed = False
        reasons.append(f"缩量市(成交额/均额={amount/amount_ma20:.1%}<{MIN_AMOUNT_RATIO:.0%})")

    if allowed:
        reasons.append(f"市场正常(广度{breadth:.1%} 净涨停{limit_balance})")

    return {"allowed": allowed, "reasons": reasons}


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
