# -*- coding: utf-8 -*-
"""
Alpha factor lab for ETF candidates.

This script tests strategy-friendly adaptations of selected GTJA Alpha191
factors on the ETF pool. It is intentionally a lab tool, not a registered
production strategy: each factor produces a daily score, then the portfolio
holds the top N ETFs by score with a simple periodic rebalance.

Usage:
  python script/alpha_factor_lab.py
  python script/alpha_factor_lab.py --factor alpha090 --start 2024-01-01
  python script/alpha_factor_lab.py --rebalance-days 10 --max-positions 5
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.run_backtest import load_etf_bars
from backtest.strategy import strategy_etf_alpha042 as ETF_ALPHA042
from backtest.portfolio import _build_market_stats


DEFAULT_START_DATE = "2022-05-06"
DEFAULT_INITIAL_CASH = 1_000_000.0
DEFAULT_MAX_POSITIONS = 5
DEFAULT_REBALANCE_DAYS = 5
DEFAULT_MAX_HOLD_DAYS = 40
DEFAULT_MIN_AMOUNT = 1_000_000.0
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "factor_lab"
DEFAULT_FILTER_TOP_PCT = 0.30


@dataclass(frozen=True)
class FactorSpec:
    id: str
    name: str
    description: str
    compute: object


def _sma(values, window):
    out = [None] * len(values)
    if window <= 0:
        return out
    total = 0.0
    queue = []
    for i, value in enumerate(values):
        if value is None:
            queue.append(None)
        else:
            queue.append(float(value))
            total += float(value)
        if len(queue) > window:
            old = queue.pop(0)
            if old is not None:
                total -= old
        if len(queue) == window and all(v is not None for v in queue):
            out[i] = total / window
    return out


def _rolling_sum(values, window):
    out = [None] * len(values)
    total = 0.0
    queue = []
    valid_count = 0
    for i, value in enumerate(values):
        queue.append(value)
        if value is not None:
            total += value
            valid_count += 1
        if len(queue) > window:
            old = queue.pop(0)
            if old is not None:
                total -= old
                valid_count -= 1
        if len(queue) == window and valid_count == window:
            out[i] = total
    return out


def _rolling_high(values, window):
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = values[i + 1 - window:i + 1]
        if all(v is not None for v in chunk):
            out[i] = max(chunk)
    return out


def _rolling_low(values, window):
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = values[i + 1 - window:i + 1]
        if all(v is not None for v in chunk):
            out[i] = min(chunk)
    return out


def _rolling_std(values, window):
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = values[i + 1 - window:i + 1]
        if not all(v is not None for v in chunk):
            continue
        mean = sum(chunk) / window
        out[i] = (sum((v - mean) ** 2 for v in chunk) / window) ** 0.5
    return out


def _rolling_corr(x, y, window):
    out = [None] * len(x)
    for i in range(window - 1, len(x)):
        pairs = [
            (xv, yv)
            for xv, yv in zip(x[i + 1 - window:i + 1], y[i + 1 - window:i + 1])
            if xv is not None and yv is not None
        ]
        n = len(pairs)
        if n < 3:
            continue
        sx = sum(v[0] for v in pairs)
        sy = sum(v[1] for v in pairs)
        sxy = sum(v[0] * v[1] for v in pairs)
        sx2 = sum(v[0] ** 2 for v in pairs)
        sy2 = sum(v[1] ** 2 for v in pairs)
        denom = (n * sx2 - sx ** 2) * (n * sy2 - sy ** 2)
        out[i] = 0.0 if denom <= 0 else (n * sxy - sx * sy) / (denom ** 0.5)
    return out


def _rolling_cov(x, y, window):
    out = [None] * len(x)
    for i in range(window - 1, len(x)):
        pairs = [
            (xv, yv)
            for xv, yv in zip(x[i + 1 - window:i + 1], y[i + 1 - window:i + 1])
            if xv is not None and yv is not None
        ]
        n = len(pairs)
        if n < 3:
            continue
        mx = sum(v[0] for v in pairs) / n
        my = sum(v[1] for v in pairs) / n
        out[i] = sum((xv - mx) * (yv - my) for xv, yv in pairs) / n
    return out


def _delta(values, period):
    out = [None] * len(values)
    for i in range(period, len(values)):
        if values[i] is not None and values[i - period] is not None:
            out[i] = values[i] - values[i - period]
    return out


def _ts_rank(values, window):
    """Time-series percentile rank of the latest value in its trailing window."""
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = values[i + 1 - window:i + 1]
        if not all(v is not None for v in chunk):
            continue
        current = chunk[-1]
        below = sum(1 for v in chunk if v < current)
        equal = sum(1 for v in chunk if v == current)
        out[i] = (below + 0.5 * equal) / window
    return out


def _safe_div(a, b):
    if a is None or b is None or abs(b) < 1e-12:
        return None
    return a / b


def _bar_arrays(bars):
    close = [b["close"] for b in bars]
    open_ = [b["open"] for b in bars]
    high = [b["high"] for b in bars]
    low = [b["low"] for b in bars]
    volume = [b.get("volume") or 0 for b in bars]
    amount = [b.get("amount") or 0 for b in bars]
    vwap = [
        (amount[i] / volume[i] if volume[i] else close[i])
        for i in range(len(bars))
    ]
    return open_, high, low, close, volume, amount, vwap


def _score_alpha062(bars):
    _, high, _, _, volume, _, _ = _bar_arrays(bars)
    vol_rank = _ts_rank(volume, 5)
    corr = _rolling_corr(high, vol_rank, 5)
    return [None if v is None else -v for v in corr]


def _score_alpha032(bars):
    _, high, _, _, volume, _, _ = _bar_arrays(bars)
    high_rank = _ts_rank(high, 3)
    vol_rank = _ts_rank(volume, 3)
    corr = _rolling_corr(high_rank, vol_rank, 3)
    rolling = _rolling_sum(corr, 3)
    return [None if v is None else -v for v in rolling]


def _score_alpha083(bars):
    _, high, _, _, volume, _, _ = _bar_arrays(bars)
    high_rank = _ts_rank(high, 5)
    vol_rank = _ts_rank(volume, 5)
    cov = _rolling_cov(high_rank, vol_rank, 5)
    return [None if v is None else -v for v in cov]


def _score_alpha090(bars):
    _, _, _, _, volume, _, vwap = _bar_arrays(bars)
    vwap_rank = _ts_rank(vwap, 5)
    vol_rank = _ts_rank(volume, 5)
    corr = _rolling_corr(vwap_rank, vol_rank, 5)
    return [None if v is None else -v for v in corr]


def _score_alpha104(bars):
    _, high, _, close, volume, _, _ = _bar_arrays(bars)
    corr = _rolling_corr(high, volume, 5)
    corr_delta = _delta(corr, 5)
    close_std = _rolling_std(close, 20)
    return [
        None if corr_delta[i] is None or close_std[i] is None else -corr_delta[i] * close_std[i]
        for i in range(len(bars))
    ]


def _score_alpha141(bars):
    _, high, _, _, volume, _, _ = _bar_arrays(bars)
    high_rank = _ts_rank(high, 9)
    mean_vol_15 = _sma(volume, 15)
    mean_vol_rank = _ts_rank(mean_vol_15, 9)
    corr = _rolling_corr(high_rank, mean_vol_rank, 9)
    return [None if v is None else -v for v in corr]


def _score_alpha176(bars):
    _, high, low, close, volume, _, _ = _bar_arrays(bars)
    high_12 = _rolling_high(high, 12)
    low_12 = _rolling_low(low, 12)
    pos = []
    for i in range(len(bars)):
        spread = None if high_12[i] is None or low_12[i] is None else high_12[i] - low_12[i]
        pos.append(None if spread is None or spread <= 0 else (close[i] - low_12[i]) / spread)
    corr = _rolling_corr(pos, volume, 6)
    return [None if v is None else -v for v in corr]


def _score_alpha088(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    out = [None] * len(bars)
    for i in range(20, len(bars)):
        out[i] = _safe_div(close[i], close[i - 20])
    return out


def _score_alpha053(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    up = [None] * len(bars)
    for i in range(1, len(bars)):
        up[i] = 1.0 if close[i] > close[i - 1] else 0.0
    up_sum = _rolling_sum(up, 12)
    return [None if v is None else v / 12.0 for v in up_sum]


def _score_alpha177(bars):
    _, high, _, close, _, _, _ = _bar_arrays(bars)
    high_20 = _rolling_high(high, 20)
    return [
        None if high_20[i] is None or high_20[i] <= 0 else close[i] / high_20[i]
        for i in range(len(bars))
    ]


def _score_alpha014(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    return _delta(close, 5)


def _score_alpha018(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    out = [None] * len(bars)
    for i in range(5, len(bars)):
        out[i] = _safe_div(close[i], close[i - 5])
    return out


def _score_alpha020(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    out = [None] * len(bars)
    for i in range(6, len(bars)):
        out[i] = _safe_div(close[i], close[i - 6])
    return out


def _score_alpha024(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    ma_5 = _sma(close, 5)
    return [
        None if ma_5[i] is None else close[i] - ma_5[i]
        for i in range(len(bars))
    ]


def _score_alpha031(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    ma_12 = _sma(close, 12)
    return [
        None if ma_12[i] is None or ma_12[i] <= 0 else close[i] / ma_12[i] - 1
        for i in range(len(bars))
    ]


def _score_alpha040(bars):
    _, _, _, close, volume, _, _ = _bar_arrays(bars)
    up_vol = [None] * len(bars)
    down_vol = [None] * len(bars)
    for i in range(1, len(bars)):
        up_vol[i] = volume[i] if close[i] > close[i - 1] else 0.0
        down_vol[i] = volume[i] if close[i] <= close[i - 1] else 0.0
    up_sum = _rolling_sum(up_vol, 26)
    down_sum = _rolling_sum(down_vol, 26)
    return [_safe_div(up_sum[i], down_sum[i]) for i in range(len(bars))]


def _score_alpha042(bars):
    """alpha042: 量价背离 — -corr(high, volume, 10)
    正值 = 价涨量缩（筹码锁定、看多）; 负值 = 价量同步（散户追涨、看空）
    """
    _, high, _, close, volume, _, _ = _bar_arrays(bars)
    corr_high_vol = _rolling_corr(high, volume, 10)
    return [-v if v is not None else None for v in corr_high_vol]


def _score_alpha043(bars):
    _, _, _, close, volume, _, _ = _bar_arrays(bars)
    obv_flow = [None] * len(bars)
    for i in range(1, len(bars)):
        if close[i] > close[i - 1]:
            obv_flow[i] = volume[i]
        elif close[i] < close[i - 1]:
            obv_flow[i] = -volume[i]
        else:
            obv_flow[i] = 0.0
    obv = _rolling_sum(obv_flow, 6)
    vol_sum = _rolling_sum(volume, 6)
    return [_safe_div(obv[i], vol_sum[i]) for i in range(len(bars))]


def _score_alpha058(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    up = [None] * len(bars)
    for i in range(1, len(bars)):
        up[i] = 1.0 if close[i] > close[i - 1] else 0.0
    up_sum = _rolling_sum(up, 20)
    return [None if v is None else v / 20.0 for v in up_sum]


def _score_alpha066(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    ma_6 = _sma(close, 6)
    return [
        None if ma_6[i] is None or ma_6[i] <= 0 else close[i] / ma_6[i] - 1
        for i in range(len(bars))
    ]


def _score_alpha071(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    ma_24 = _sma(close, 24)
    return [
        None if ma_24[i] is None or ma_24[i] <= 0 else close[i] / ma_24[i] - 1
        for i in range(len(bars))
    ]


def _score_alpha084(bars):
    _, _, _, close, volume, _, _ = _bar_arrays(bars)
    obv_flow = [None] * len(bars)
    for i in range(1, len(bars)):
        if close[i] > close[i - 1]:
            obv_flow[i] = volume[i]
        elif close[i] < close[i - 1]:
            obv_flow[i] = -volume[i]
        else:
            obv_flow[i] = 0.0
    obv = _rolling_sum(obv_flow, 20)
    vol_sum = _rolling_sum(volume, 20)
    return [_safe_div(obv[i], vol_sum[i]) for i in range(len(bars))]


def _score_alpha102(bars):
    _, _, _, _, volume, _, _ = _bar_arrays(bars)
    gains = [None] * len(bars)
    losses = [None] * len(bars)
    for i in range(1, len(bars)):
        diff = volume[i] - volume[i - 1]
        gains[i] = max(diff, 0)
        losses[i] = max(-diff, 0)
    gain_sum = _rolling_sum(gains, 6)
    loss_sum = _rolling_sum(losses, 6)
    out = [None] * len(bars)
    for i in range(len(bars)):
        if gain_sum[i] is None or loss_sum[i] is None:
            continue
        denom = gain_sum[i] + loss_sum[i]
        out[i] = None if denom <= 0 else gain_sum[i] / denom
    return out


def _score_alpha106(bars):
    _, _, _, close, _, _, _ = _bar_arrays(bars)
    return _delta(close, 20)


def _score_alpha127(bars):
    _, high, _, close, _, _, _ = _bar_arrays(bars)
    high_12 = _rolling_high(high, 12)
    return [
        None if high_12[i] is None or high_12[i] <= 0 else -(high_12[i] / close[i] - 1) ** 2
        for i in range(len(bars))
    ]


def _score_alpha134(bars):
    _, _, _, close, volume, _, _ = _bar_arrays(bars)
    out = [None] * len(bars)
    for i in range(12, len(bars)):
        out[i] = (close[i] / close[i - 12] - 1) * volume[i]
    return out


def _score_alpha161(bars):
    out = [None] * len(bars)
    tr_values = []
    for i, bar in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i > 0 else bar["close"]
        tr = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - prev_close),
            abs(bar["low"] - prev_close),
        )
        tr_values.append(tr / bar["close"] if bar["close"] else None)
    atr_12 = _sma(tr_values, 12)
    for i, value in enumerate(atr_12):
        out[i] = None if value is None else -value
    return out


def _score_alpha188(bars):
    _, high, low, close, _, _, _ = _bar_arrays(bars)
    amp = [
        None if close[i] <= 0 else (high[i] - low[i]) / close[i]
        for i in range(len(bars))
    ]
    amp_ma_11 = _sma(amp, 11)
    return [
        None if amp[i] is None or amp_ma_11[i] is None else -(amp[i] - amp_ma_11[i])
        for i in range(len(bars))
    ]


FACTOR_SPECS = [
    FactorSpec("alpha014", "5日动量差", "close - delay(close,5)", _score_alpha014),
    FactorSpec("alpha018", "5日价格比", "close / delay(close,5)", _score_alpha018),
    FactorSpec("alpha020", "6日价格比", "close / delay(close,6)", _score_alpha020),
    FactorSpec("alpha024", "5日均线偏离", "close - mean(close,5)", _score_alpha024),
    FactorSpec("alpha031", "12日乖离率", "close / mean(close,12) - 1", _score_alpha031),
    FactorSpec("alpha040", "上涨量下跌量比", "sum(up volume,26) / sum(down volume,26)", _score_alpha040),
    FactorSpec("alpha042", "量价背离(042)", "-corr(high, volume, 10) 正值=价涨量缩看多", _score_alpha042),
    FactorSpec("alpha043", "6日OBV强度", "sum(signed volume,6) / sum(volume,6)", _score_alpha043),
    FactorSpec("alpha058", "20日上涨占比", "count(close > delay(close,1),20) / 20", _score_alpha058),
    FactorSpec("alpha066", "6日乖离率", "close / mean(close,6) - 1", _score_alpha066),
    FactorSpec("alpha071", "24日乖离率", "close / mean(close,24) - 1", _score_alpha071),
    FactorSpec("alpha062", "高点-成交量短背离", "-corr(high, ts_rank(volume,5), 5)", _score_alpha062),
    FactorSpec("alpha032", "高点-成交量相关累计", "-sum(corr(ts_rank(high,3), ts_rank(volume,3),3),3)", _score_alpha032),
    FactorSpec("alpha083", "高点-成交量协方差背离", "-cov(ts_rank(high,5), ts_rank(volume,5),5)", _score_alpha083),
    FactorSpec("alpha090", "VWAP-成交量背离", "-corr(ts_rank(vwap,5), ts_rank(volume,5),5)", _score_alpha090),
    FactorSpec("alpha104", "量价相关变化率", "-delta(corr(high,volume,5),5) * std(close,20)", _score_alpha104),
    FactorSpec("alpha141", "高点-均量背离", "-corr(ts_rank(high,9), ts_rank(mean(volume,15),9),9)", _score_alpha141),
    FactorSpec("alpha176", "区间位置-成交量背离", "-corr(close position in 12d range, volume,6)", _score_alpha176),
    FactorSpec("alpha088", "20日动量", "close / delay(close,20)", _score_alpha088),
    FactorSpec("alpha053", "12日上涨占比", "count(close > delay(close,1),12) / 12", _score_alpha053),
    FactorSpec("alpha177", "接近20日高点", "close / high_20", _score_alpha177),
    FactorSpec("alpha084", "20日OBV强度", "sum(signed volume,20) / sum(volume,20)", _score_alpha084),
    FactorSpec("alpha102", "成交量RSI", "volume gain / (volume gain + volume loss), 6d", _score_alpha102),
    FactorSpec("alpha106", "20日动量差", "close - delay(close,20)", _score_alpha106),
    FactorSpec("alpha127", "12日高点回撤惩罚", "-(high_12 / close - 1)^2", _score_alpha127),
    FactorSpec("alpha134", "12日量价动量", "12d return * volume", _score_alpha134),
    FactorSpec("alpha161", "低ATR偏好", "-ATR(12) / close", _score_alpha161),
    FactorSpec("alpha188", "振幅低于均值", "-(amplitude - mean(amplitude,11))", _score_alpha188),
]
FACTOR_BY_ID = {f.id: f for f in FACTOR_SPECS}


def _max_drawdown(equity_values):
    peak = None
    max_dd = 0.0
    for value in equity_values:
        peak = value if peak is None else max(peak, value)
        if peak:
            max_dd = min(max_dd, (value - peak) / peak * 100)
    return abs(max_dd)


def _profit_factor(trades):
    wins = sum(t["profit"] for t in trades if t["profit"] > 0)
    losses = abs(sum(t["profit"] for t in trades if t["profit"] <= 0))
    if losses == 0:
        return 999 if wins > 0 else None
    return round(wins / losses, 2)


def _build_score_maps(bars_by_code, factor):
    score_by_code_date = {}
    all_dates = set()
    last_price = {}
    for code, bars in bars_by_code.items():
        scores = factor.compute(bars)
        code_scores = {}
        code_prices = {}
        for bar, score in zip(bars, scores):
            date = bar["trade_date"]
            all_dates.add(date)
            code_prices[date] = bar["close"]
            if score is not None and math.isfinite(score):
                code_scores[date] = score
        score_by_code_date[code] = code_scores
        last_price[code] = code_prices
    return sorted(all_dates), score_by_code_date, last_price


def run_factor_backtest(
    factor,
    stocks,
    bars_by_code,
    initial_cash=DEFAULT_INITIAL_CASH,
    max_positions=DEFAULT_MAX_POSITIONS,
    rebalance_days=DEFAULT_REBALANCE_DAYS,
    max_hold_days=DEFAULT_MAX_HOLD_DAYS,
    min_amount=DEFAULT_MIN_AMOUNT,
):
    stock_map = {s["code"]: s for s in stocks}
    dates, score_by_code_date, price_by_code_date = _build_score_maps(bars_by_code, factor)
    bar_lookup = {
        code: {b["trade_date"]: b for b in bars}
        for code, bars in bars_by_code.items()
    }

    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    rebalance_index = 0

    for date in dates:
        # Update equity first with latest visible prices.
        equity = cash
        for code, pos in positions.items():
            price = price_by_code_date.get(code, {}).get(date, pos["buy_price"])
            equity += pos["shares"] * price
        equity_curve.append({
            "date": date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_count": len(positions),
        })

        if rebalance_index % rebalance_days != 0:
            rebalance_index += 1
            continue
        rebalance_index += 1

        candidates = []
        for code, scores in score_by_code_date.items():
            score = scores.get(date)
            bar = bar_lookup.get(code, {}).get(date)
            if score is None or not bar:
                continue
            if (bar.get("amount") or 0) < min_amount:
                continue
            candidates.append((score, code, bar))
        candidates.sort(reverse=True, key=lambda x: x[0])
        target_codes = {code for _, code, _ in candidates[:max_positions]}

        # Sell names that fell out of the top list or aged out.
        for code in list(positions):
            pos = positions[code]
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            hold_days = pos["hold_days"] + rebalance_days
            sell_reason = None
            if code not in target_codes:
                sell_reason = f"rank_out({factor.id})"
            elif hold_days >= max_hold_days:
                sell_reason = f"max_hold_{max_hold_days}d"
            if sell_reason is None:
                pos["hold_days"] = hold_days
                continue
            sell_price = float(bar["close"])
            income = pos["shares"] * sell_price
            cost = pos["shares"] * pos["buy_price"]
            cash += income
            trades.append({
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "sell_date": date,
                "sell_price": round(sell_price, 3),
                "shares": pos["shares"],
                "profit": round(income - cost, 2),
                "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
                "buy_reason": pos["buy_reason"],
                "sell_reason": sell_reason,
            })
            del positions[code]

        # Buy missing targets, equal-weighting remaining cash across slots.
        for score, code, bar in candidates[:max_positions]:
            if code in positions or len(positions) >= max_positions:
                continue
            remaining_slots = max(1, max_positions - len(positions))
            budget = cash / remaining_slots
            price = float(bar["close"])
            shares = int(budget // price // 100 * 100)
            if shares <= 0:
                continue
            cost = shares * price
            cash -= cost
            positions[code] = {
                "buy_date": date,
                "buy_price": price,
                "shares": shares,
                "hold_days": 0,
                "buy_reason": f"{factor.id} score={score:.4f}",
            }

    last_date = dates[-1] if dates else None
    for code, pos in positions.items():
        sell_price = price_by_code_date.get(code, {}).get(last_date, pos["buy_price"])
        income = pos["shares"] * sell_price
        cost = pos["shares"] * pos["buy_price"]
        trades.append({
            "code": code,
            "name": stock_map.get(code, {"name": code})["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(sell_price, 3),
            "shares": pos["shares"],
            "profit": round(income - cost, 2),
            "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    closed_trades = [t for t in trades if t["sell_reason"] != "期末持仓"]
    wins = [t for t in closed_trades if t["profit"] > 0]
    avg_profit_pct = (
        sum(t["profit_pct"] for t in closed_trades) / len(closed_trades)
        if closed_trades else 0.0
    )
    summary = {
        "factor_id": factor.id,
        "factor_name": factor.name,
        "description": factor.description,
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
        "max_drawdown_pct": round(_max_drawdown([x["equity"] for x in equity_curve]), 2),
        "trade_count": len(closed_trades),
        "win_rate_pct": round(len(wins) / max(1, len(closed_trades)) * 100, 2),
        "avg_profit_pct": round(avg_profit_pct, 2),
        "profit_factor": _profit_factor(closed_trades),
        "open_positions": len(positions),
    }
    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "current_positions": [
            {
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "cur_price": round(price_by_code_date.get(code, {}).get(last_date, pos["buy_price"]), 3),
                "shares": pos["shares"],
            }
            for code, pos in positions.items()
        ],
    }


def _build_factor_rank_by_date(factor, bars_by_code, min_amount):
    _, score_by_code_date, _ = _build_score_maps(bars_by_code, factor)
    by_date = {}
    bar_lookup = {
        code: {b["trade_date"]: b for b in bars}
        for code, bars in bars_by_code.items()
    }
    for code, scores in score_by_code_date.items():
        for date, score in scores.items():
            bar = bar_lookup.get(code, {}).get(date)
            if not bar or (bar.get("amount") or 0) < min_amount:
                continue
            by_date.setdefault(date, []).append((score, code))

    rank_by_date = {}
    for date, items in by_date.items():
        items.sort(reverse=True, key=lambda x: x[0])
        n = len(items)
        if not n:
            continue
        rank_by_date[date] = {
            code: {
                "score": score,
                "rank": idx + 1,
                "pct": (idx + 1) / n,
            }
            for idx, (score, code) in enumerate(items)
        }
    return rank_by_date


def run_alpha042_filter_backtest(
    factor,
    stocks,
    bars_by_code,
    initial_cash=DEFAULT_INITIAL_CASH,
    max_positions=DEFAULT_MAX_POSITIONS,
    min_amount=DEFAULT_MIN_AMOUNT,
    filter_top_pct=DEFAULT_FILTER_TOP_PCT,
):
    """Run ETF Alpha042 signals, but filter/sort buys by a factor rank.

    A buy signal is allowed only when the factor rank is within top
    filter_top_pct for that date. Among allowed buys, higher factor score wins.
    Sell signals remain the original Alpha042 exits.
    """
    stock_map = {s["code"]: s for s in stocks}
    market_stats = _build_market_stats(bars_by_code)
    factor_rank_by_date = _build_factor_rank_by_date(factor, bars_by_code, min_amount)

    signal_by_date = {}
    bar_lookup = {}
    all_dates = set()
    for code, bars in bars_by_code.items():
        clean_bars = [b for b in bars if b.get("close") and b.get("open")]
        if len(clean_bars) < 30:
            continue
        bar_lookup[code] = {b["trade_date"]: b for b in clean_bars}
        all_dates.update(bar_lookup[code].keys())
        for signal in ETF_ALPHA042.generate_signals(clean_bars):
            signal_by_date.setdefault(signal["date"], []).append({
                "code": code,
                "action": signal["action"],
                "reason": signal.get("reason", ""),
            })

    dates = sorted(all_dates)
    cash = float(initial_cash)
    positions = {}
    trades = []
    equity_curve = []
    last_price = {}
    gate_history = []

    for date in dates:
        for code, lookup in bar_lookup.items():
            if date in lookup:
                last_price[code] = float(lookup[date]["close"])

        todays = signal_by_date.get(date, [])
        sell_signals = [s for s in todays if s["action"] == "sell"]
        buy_signals = [s for s in todays if s["action"] == "buy"]

        gate = ETF_ALPHA042.market_gate(date, market_stats)
        gate_history.append({"date": date, **gate})
        if not gate["allowed"]:
            buy_signals = []

        for signal in sell_signals:
            code = signal["code"]
            pos = positions.get(code)
            bar = bar_lookup.get(code, {}).get(date)
            if not pos or not bar or pos["buy_date"] == date:
                continue
            sell_price = float(bar["close"])
            income = pos["shares"] * sell_price
            cost = pos["shares"] * pos["buy_price"]
            cash += income
            trades.append({
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "sell_date": date,
                "sell_price": round(sell_price, 3),
                "shares": pos["shares"],
                "profit": round(income - cost, 2),
                "profit_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
                "buy_reason": pos["buy_reason"],
                "sell_reason": signal["reason"] or "Alpha042卖出",
            })
            del positions[code]

        candidates = []
        ranks = factor_rank_by_date.get(date, {})
        for signal in buy_signals:
            code = signal["code"]
            if code in positions or len(positions) >= max_positions:
                continue
            rank_item = ranks.get(code)
            if not rank_item or rank_item["pct"] > filter_top_pct:
                continue
            bar = bar_lookup.get(code, {}).get(date)
            if not bar:
                continue
            candidates.append((rank_item["score"], signal, bar, rank_item))
        candidates.sort(reverse=True, key=lambda x: x[0])

        for _, signal, bar, rank_item in candidates:
            if len(positions) >= max_positions:
                break
            remaining_slots = max(1, max_positions - len(positions))
            budget = cash / remaining_slots
            price = float(bar["close"])
            shares = int(budget // price // 100 * 100)
            if shares <= 0:
                continue
            cost = shares * price
            cash -= cost
            positions[signal["code"]] = {
                "buy_date": date,
                "buy_price": price,
                "shares": shares,
                "buy_reason": (
                    f"{signal['reason']} | {factor.id} rank={rank_item['rank']} "
                    f"pct={rank_item['pct']:.2f} score={rank_item['score']:.4f}"
                ),
            }

        equity = cash
        for code, pos in positions.items():
            equity += pos["shares"] * last_price.get(code, pos["buy_price"])
        equity_curve.append({
            "date": date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "position_count": len(positions),
        })

    last_date = dates[-1] if dates else None
    for code, pos in positions.items():
        last_px = last_price.get(code, pos["buy_price"])
        cost = pos["shares"] * pos["buy_price"]
        cur_val = pos["shares"] * last_px
        trades.append({
            "code": code,
            "name": stock_map.get(code, {"name": code})["name"],
            "buy_date": pos["buy_date"],
            "buy_price": round(pos["buy_price"], 3),
            "sell_date": last_date,
            "sell_price": round(last_px, 3),
            "shares": pos["shares"],
            "profit": round(cur_val - cost, 2),
            "profit_pct": round((last_px / pos["buy_price"] - 1) * 100, 2),
            "buy_reason": pos["buy_reason"],
            "sell_reason": "期末持仓",
        })

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    closed_trades = [t for t in trades if t["sell_reason"] != "期末持仓"]
    wins = [t for t in closed_trades if t["profit"] > 0]
    avg_profit_pct = (
        sum(t["profit_pct"] for t in closed_trades) / len(closed_trades)
        if closed_trades else 0.0
    )
    summary = {
        "factor_id": f"alpha042_{factor.id}",
        "factor_name": f"Alpha042 + {factor.name}",
        "description": f"Alpha042 buy signals filtered by top {filter_top_pct:.0%} {factor.id}",
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity - initial_cash) / initial_cash * 100, 2),
        "max_drawdown_pct": round(_max_drawdown([x["equity"] for x in equity_curve]), 2),
        "trade_count": len(closed_trades),
        "win_rate_pct": round(len(wins) / max(1, len(closed_trades)) * 100, 2),
        "avg_profit_pct": round(avg_profit_pct, 2),
        "profit_factor": _profit_factor(closed_trades),
        "open_positions": len(positions),
        "allowed_days": sum(1 for x in gate_history if x.get("allowed")),
        "blocked_days": sum(1 for x in gate_history if not x.get("allowed")),
    }
    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "current_positions": [
            {
                "code": code,
                "name": stock_map.get(code, {"name": code})["name"],
                "buy_date": pos["buy_date"],
                "buy_price": round(pos["buy_price"], 3),
                "cur_price": round(last_price.get(code, pos["buy_price"]), 3),
                "shares": pos["shares"],
            }
            for code, pos in positions.items()
        ],
    }


def _save_results(results, args):
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DEFAULT_OUTPUT_DIR / f"alpha_factor_lab_{stamp}.json"
    csv_path = DEFAULT_OUTPUT_DIR / f"alpha_factor_lab_{stamp}.csv"
    payload = {
        "params": vars(args),
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        fieldnames = [
            "factor_id", "factor_name", "total_return_pct", "max_drawdown_pct",
            "win_rate_pct", "trade_count", "avg_profit_pct", "profit_factor",
            "final_equity", "open_positions",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            writer.writerow({k: item["summary"].get(k) for k in fieldnames})
    return json_path, csv_path


def parse_args():
    parser = argparse.ArgumentParser(description="Run ETF Alpha191 candidate factor lab.")
    parser.add_argument("--factor", default="all", help="Factor id, comma list, or all")
    parser.add_argument(
        "--mode",
        choices=("single", "alpha042_filter"),
        default="single",
        help="single: factor-only rotation; alpha042_filter: filter ETF Alpha042 buys by factor rank",
    )
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH)
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS)
    parser.add_argument("--rebalance-days", type=int, default=DEFAULT_REBALANCE_DAYS)
    parser.add_argument("--max-hold-days", type=int, default=DEFAULT_MAX_HOLD_DAYS)
    parser.add_argument("--min-amount", type=float, default=DEFAULT_MIN_AMOUNT)
    parser.add_argument("--filter-top-pct", type=float, default=DEFAULT_FILTER_TOP_PCT)
    parser.add_argument("--list", action="store_true", help="List available factors")
    parser.add_argument("--no-save", action="store_true", help="Do not write JSON/CSV output")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.list:
        for factor in FACTOR_SPECS:
            print(f"{factor.id:10s} {factor.name:18s} {factor.description}")
        return

    if args.factor == "all":
        factors = FACTOR_SPECS
    else:
        factors = []
        for factor_id in [x.strip() for x in args.factor.split(",") if x.strip()]:
            if factor_id not in FACTOR_BY_ID:
                raise SystemExit(f"unknown factor: {factor_id}")
            factors.append(FACTOR_BY_ID[factor_id])

    stocks, bars_by_code = load_etf_bars(start_date=args.start, end_date=args.end)
    print(
        f"\n{'factor':10s} {'return':>9s} {'dd':>7s} {'win':>7s} "
        f"{'trades':>7s} {'avg':>7s} {'pf':>7s} {'open':>5s}"
    )
    print("-" * 72)
    results = []
    for factor in factors:
        if args.mode == "alpha042_filter":
            result = run_alpha042_filter_backtest(
                factor,
                stocks,
                bars_by_code,
                initial_cash=args.initial_cash,
                max_positions=args.max_positions,
                min_amount=args.min_amount,
                filter_top_pct=args.filter_top_pct,
            )
        else:
            result = run_factor_backtest(
                factor,
                stocks,
                bars_by_code,
                initial_cash=args.initial_cash,
                max_positions=args.max_positions,
                rebalance_days=args.rebalance_days,
                max_hold_days=args.max_hold_days,
                min_amount=args.min_amount,
            )
        results.append(result)
        s = result["summary"]
        pf = s["profit_factor"]
        pf_text = "None" if pf is None else f"{pf:.2f}"
        print(
            f"{s['factor_id']:10s} {s['total_return_pct']:>+8.2f}% "
            f"{s['max_drawdown_pct']:>6.2f}% {s['win_rate_pct']:>6.2f}% "
            f"{s['trade_count']:>7d} {s['avg_profit_pct']:>+6.2f}% "
            f"{pf_text:>7s} {s['open_positions']:>5d}"
        )

    results.sort(
        key=lambda r: (
            r["summary"]["profit_factor"] or 0,
            r["summary"]["total_return_pct"],
        ),
        reverse=True,
    )
    if not args.no_save:
        json_path, csv_path = _save_results(results, args)
        print(f"\nSaved: {json_path}")
        print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
