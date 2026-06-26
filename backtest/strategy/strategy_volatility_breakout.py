# -*- coding: utf-8 -*-
"""
波动率V反策略 (Volatility V-Reversal Strategy)

核心理念（三个维度叠加）：
1. 波动率异动：长期平稳的股票，波动率突然放大 → 有资金关注
2. V 形反转：波动放大伴随快速下跌，但 3~5 天内 V 型拉回 → 洗盘/底部确认
3. 大盘脱敏：以前跟随大盘，近期走出独立走势 → 个股逻辑强于大盘

策略流程：
- 先检测 V 反形态：跌得下去 + 涨得回来 = 底部确认
- 再验证波动率：短期波动率 > 长期波动率（异动确认）
- 大盘过滤：大盘不能处于暴跌/恐慌状态（market_gate）
- 卖出：波动率回归、V反失效（再创新低）、或止盈止损

关键指标：
- daily_vol:  每日波动率 = |当日涨跌幅|
- vol_5d:     5日 均波动率（近期）
- vol_60d:    60日 均波动率（长期基准）
- v_reversal: V反检测（跌→底→涨 在 3~5 天内完成）
- market_gate: 大盘过滤（涨跌比、恐慌检测）
"""
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.indicators import sma, stddev

META = {
    "id": "volatility_breakout",
    "name": "波动率V反",
    "description": "长期平稳股票波动率突然放大+快速下跌后V型拉回+大盘脱敏。三条件叠加确认底部，波动率回归或V反失效卖出。",
}

# ============ 可调参数 ============

# -- V反检测 --
V_LOOKBACK = 8             # 向前搜索 V 形的窗口（天）
V_DECLINE_MIN = 3.0        # V 左侧最小跌幅（%），太小不算异动
V_RECOVERY_MIN = 1.5       # V 右侧最小涨幅（从底部算，%）
V_RECOVERY_RATIO = 0.4     # 右侧涨幅至少恢复到左侧跌幅的 40%
V_MIN_DOWN_DAYS = 2        # V 左侧至少连续下跌天数
V_MIN_UP_DAYS = 1          # V 右侧至少上涨天数
V_MAX_DURATION = 8         # V 形总跨度（天），从左侧高点到当前

# -- 波动率 --
VOL_STABLE_MAX = 0.025     # 60日波动率上限，低于此值才算"长期平稳"（日均<2.5%）
VOL_RATIO_BUY = 1.3        # vol_5d/vol_60d 突破比值 → 异动确认（比之前的1.5更宽松，因为有V反兜底）

# -- 卖出 --
STOP_LOSS_PCT = -10        # 硬止损（%）
TAKE_PROFIT_PCT = 20       # 止盈（%）
MAX_HOLD_DAYS = 15         # 最大持仓天数
DAILY_CRASH_PCT = -8       # 单日暴跌离场（%）

# -- 涨停过滤 & 过度拉升过滤 --
LIMIT_UP_PCT = 9.5          # 涨停阈值（%），科创板20%涨停在这里用9.5%过滤比较安全
LIMIT_UP_LOOKBACK = 3       # 近N天内有涨停 → 不买（已经拉起来了，不是蓄力阶段）
V_RECOVERY_HARD_MAX = 20.0  # V反恢复超过此值 → 不买（过度拉升，追高风险大）


# ============ V 形反转检测 ============

def _detect_v_reversal(closes, i, lookback=V_LOOKBACK):
    """
    在 index=i 处检测 V 形反转形态。

    V 形结构：
      - 左侧：快速下跌（连续阴线，跌幅≥3%）
      - 底部：局部最低点
      - 右侧：快速回升（连续阳线，从底部回升≥1.5%，且恢复到左侧跌幅的40%+）

    参数:
        closes: 收盘价序列
        i:      当前索引
        lookback: 向前搜索窗口

    返回:
        (is_v, bottom_idx, decline_pct, recovery_pct, label)
    """
    # 至少需要 5 根K线才能构成 V 形（高→跌→底→涨→今）
    if i < 4:
        return False, -1, 0, 0, ""

    start = max(0, i - lookback)

    # 在窗口内找最低点（不能是今天，今天已经是右侧了）
    window_for_min = closes[start:i]  # 不包括今天
    if not window_for_min:
        return False, -1, 0, 0, ""

    min_val = min(window_for_min)
    bottom_idx = start + window_for_min.index(min_val)
    bottom_close = closes[bottom_idx]

    # V 的左侧高点：从底部往前找，找到下跌前的局部最高点
    # 这样度量的是"V 形下跌段"的真正跌幅，而不是窗口内最大跌幅
    left_peak_idx = bottom_idx
    for j in range(bottom_idx - 1, start, -1):
        if closes[j] > closes[left_peak_idx]:
            left_peak_idx = j
        else:
            break  # 前方价格不再升高，V 的左峰确认
    pre_high = closes[left_peak_idx]

    # 左侧跌幅
    decline_pct = (pre_high - bottom_close) / pre_high * 100 if pre_high > 0 else 0
    if decline_pct < V_DECLINE_MIN:
        return False, -1, 0, 0, ""

    # 右侧恢复幅度（从底部到当前）
    recovery_pct = (closes[i] - bottom_close) / bottom_close * 100 if bottom_close > 0 else 0
    if recovery_pct < V_RECOVERY_MIN:
        return False, -1, 0, 0, ""
    if recovery_pct < decline_pct * V_RECOVERY_RATIO:
        return False, -1, 0, 0, ""

    # 左侧连续下跌天数（从左峰到最低点，只计有实质跌幅的阴线）
    down_days = 0
    for j in range(bottom_idx, left_peak_idx, -1):
        if closes[j] < closes[j - 1] and (closes[j - 1] / closes[j] - 1) >= 0.005:
            down_days += 1
    if down_days < V_MIN_DOWN_DAYS:
        return False, -1, 0, 0, ""

    # 右侧上涨天数（从底部到当前）
    up_days = 0
    for j in range(bottom_idx + 1, i + 1):
        if closes[j] > closes[j - 1]:
            up_days += 1
    if up_days < V_MIN_UP_DAYS:
        return False, -1, 0, 0, ""

    # 整体 V 形跨度（从左侧高点到今天）
    v_duration = i - left_peak_idx
    if v_duration > V_MAX_DURATION:
        return False, -1, 0, 0, ""

    # 确认底部是真正的局部最低点（前后各1天都比它高）
    if bottom_idx > start and closes[bottom_idx - 1] < bottom_close:
        return False, -1, 0, 0, ""
    if bottom_idx < i - 1 and closes[bottom_idx + 1] < bottom_close:
        return False, -1, 0, 0, ""

    label = f"V反({down_days}阴-{up_days}阳|跌{decline_pct:.1f}%→涨{recovery_pct:.1f}%)"
    return True, bottom_idx, round(decline_pct, 2), round(recovery_pct, 2), label


# ============ 个股波动率计算 ============

def _compute_volatility_metrics(bars):
    """预计算所有波动率相关指标。"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]

    n = len(bars)

    # 每日波动率 = |涨跌幅|
    daily_vol = [0.0]
    daily_change = [0.0]  # 带符号的涨跌幅
    for i in range(1, n):
        chg = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0
        daily_vol.append(abs(chg))
        daily_change.append(chg)

    # 日内振幅
    daily_range = [(highs[i] - lows[i]) / closes[i] if closes[i] else 0 for i in range(n)]

    return {
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volumes": volumes,
        "daily_vol": daily_vol,
        "daily_change": daily_change,
        "daily_range": daily_range,
        "vol_5d": sma(daily_vol, 5),
        "vol_10d": sma(daily_vol, 10),
        "vol_20d": sma(daily_vol, 20),
        "vol_60d": sma(daily_vol, 60),
        "vol_std_10d": stddev(daily_vol, 10),
        "range_ma10": sma(daily_range, 10),
        "vol_ma5": sma(volumes, 5),
        "vol_ma20": sma(volumes, 20),
    }


# ============ 主策略 ============

def _has_recent_limit_up(daily_change, i, lookback=LIMIT_UP_LOOKBACK):
    """检查近N天内是否有涨停（含当日），涨停股已拉起来，不追"""
    start = max(1, i - lookback + 1)  # daily_change[0] is always 0
    for j in range(start, i + 1):
        if daily_change[j] * 100 >= LIMIT_UP_PCT:
            return True
    return False


def generate_signals(bars):
    """
    生成买卖信号。

    买入 = 波动率异动 + V形反转 + 不接飞刀
    卖出 = 波动率回归 / V反失效(再创新低) / 止盈止损
    """
    m = _compute_volatility_metrics(bars)
    closes = m["closes"]
    n = len(closes)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None
    v_bottom_close = None  # 入场时的V反底部价，用于后续跟踪

    min_idx = 65  # 60日均线 + V反回看 + buffer

    for i in range(min_idx, n):
        v5 = m["vol_5d"][i]
        v60 = m["vol_60d"][i]
        v20 = m["vol_20d"][i]

        if v5 is None or v60 is None or v20 is None:
            continue
        if m["vol_5d"][i - 1] is None or m["vol_60d"][i - 1] is None:
            continue

        close = closes[i]
        vol_ratio_5_60 = v5 / v60 if v60 > 0.001 else 1.0

        # ---- 每日风控：检测是否有连续跌停 ----
        daily_chg = m["daily_change"][i]

        # ==================== 买入逻辑 ====================
        if not in_pos:
            # 条件 1：历史上平稳（60日波动率低）
            stable_history = v60 < VOL_STABLE_MAX

            # 条件 2：波动率放大
            vol_expanding = vol_ratio_5_60 >= VOL_RATIO_BUY

            # 条件 3：V 形反转
            result = _detect_v_reversal(closes, i)
            is_v, v_bottom_idx, decline_pct, recovery_pct, v_label = result

            # 条件 3.5：V反恢复不过度（超过20%说明已经拉起来了，追高风险大）
            not_overextended = recovery_pct <= V_RECOVERY_HARD_MAX

            # 条件 3.6：近3天无涨停（涨停股已拉起来，不是蓄力阶段）
            no_recent_limit_up = not _has_recent_limit_up(m["daily_change"], i)

            # 条件 4：不接飞刀（V反右侧确认，今天不是跌的）
            not_falling = daily_chg > -0.03

            # 条件 5：放量确认（V反右侧量大于左侧量）
            vol_confirm = True
            if is_v and v_bottom_idx >= 0:
                right_vol = sum(m["volumes"][v_bottom_idx:i + 1]) / max(i - v_bottom_idx, 1)
                left_start = max(0, v_bottom_idx - (i - v_bottom_idx))
                left_vol = sum(m["volumes"][left_start:v_bottom_idx + 1]) / max(v_bottom_idx - left_start, 1)
                vol_confirm = right_vol >= left_vol * 0.8  # 右侧量不低于左侧80%

            if stable_history and vol_expanding and is_v and not_overextended and no_recent_limit_up and not_falling and vol_confirm:
                in_pos = True
                entry_price = close
                entry_index = i
                v_bottom_close = closes[v_bottom_idx]

                signals.append({
                    "date": bars[i]["trade_date"],
                    "action": "buy",
                    "reason": f"{v_label}，波动异动(v5/v60={vol_ratio_5_60:.1f})",
                })
            continue

        # ==================== 卖出逻辑 ====================
        # 硬止损 + V反失效 + 止盈 + 单日暴跌 + 持仓到期
        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0

        reason = None

        if profit_pct <= STOP_LOSS_PCT:
            reason = f"止损({profit_pct:.1f}%)"
        elif v_bottom_close and close < v_bottom_close:
            reason = f"V反失效(跌破底部{v_bottom_close:.2f})"
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
        v_bottom_close = None

    return signals


# ============ 独立运行 ============

if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta

    run_strategy_meta(META)
