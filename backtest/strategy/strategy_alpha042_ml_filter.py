# -*- coding: utf-8 -*-
"""Alpha042 buy points filtered by the trained sklearn model."""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import joblib

from backtest.strategy import strategy_alpha042 as alpha042


META = {
    "id": "alpha042_ml_filter",
    "name": "Alpha042-ML买点过滤",
    "type": "stock",
    "description": "先按Alpha042产生买点，再用sklearn模型过滤高概率买点。",
}

MODEL_PATH = ROOT_DIR / "data" / "ml" / "alpha042_buy_filter" / "model.pkl"
_MODEL_BUNDLE = None

FEATURE_COLUMNS = [
    "corr_high_volume_10",
    "vol_amp",
    "near_high_pct",
    "chg_1d",
    "chg_3d",
    "chg_5d",
    "chg_10d",
    "chg_20d",
    "amount",
    "amount_ratio_20",
    "volume_ratio_20",
    "amplitude",
    "close_position",
    "close_ma5_ratio",
    "close_ma10_ratio",
    "close_ma20_ratio",
    "ma5_ma20_ratio",
    "max_drawdown_5d",
    "up_days_5d",
    "limit_up_recent_3d",
    "market_breadth",
    "market_above_ma20_ratio",
    "market_false_breakout_rate_5d",
    "market_ret_20d",
    "market_range_20d",
    "market_amount_ratio",
]


def _load_model_bundle():
    global _MODEL_BUNDLE
    if _MODEL_BUNDLE is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Alpha042 ML model not found: {MODEL_PATH}")
        _MODEL_BUNDLE = joblib.load(MODEL_PATH)
    return _MODEL_BUNDLE


def _safe_div(a, b, default=0.0):
    if b is None or abs(b) < 1e-12:
        return default
    return a / b


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _max_drawdown(closes):
    peak = None
    max_dd = 0.0
    for close in closes:
        if close is None:
            continue
        peak = close if peak is None else max(peak, close)
        if peak:
            max_dd = min(max_dd, close / peak - 1)
    return max_dd


def _ratio_from_n(closes, i, n):
    if i < n or not closes[i - n]:
        return 0.0
    return closes[i] / closes[i - n] - 1


def _is_raw_alpha042_buy(metrics, i):
    corr_val = metrics["high_vol_corr"][i]
    vol_amp = metrics["vol_amp"][i]
    high_20 = metrics["high_20"][i]
    chg_5d = metrics["chg_5d"][i]
    close = metrics["closes"][i]
    amount = metrics["amounts"][i]

    if corr_val is None or vol_amp is None or high_20 is None or chg_5d is None:
        return False

    return (
        corr_val < alpha042.CORR_BUY_MAX
        and alpha042.VOL_AMP_MIN <= vol_amp <= alpha042.VOL_AMP_MAX
        and close >= high_20 * (1 - alpha042.PRICE_NEAR_HIGH_PCT)
        and chg_5d > alpha042.CHG_5D_MIN
        and not alpha042._has_recent_limit_up(metrics["daily_change"], i)
        and amount >= alpha042.MIN_AMOUNT
    )


def _features_for(metrics, bars, i, market_stats):
    closes = metrics["closes"]
    highs = metrics["highs"]
    lows = metrics["lows"]
    volumes = metrics["volumes"]
    amounts = metrics["amounts"]
    close = closes[i]
    high = highs[i]
    low = lows[i]
    day_range = high - low
    amount_ma20 = _mean(amounts[max(0, i - 19):i + 1]) or amounts[i] or 1.0
    volume_ma20 = _mean(volumes[max(0, i - 19):i + 1]) or volumes[i] or 1.0
    ma5 = _mean(closes[max(0, i - 4):i + 1])
    ma10 = _mean(closes[max(0, i - 9):i + 1])
    ma20 = _mean(closes[max(0, i - 19):i + 1])
    recent_changes = metrics["daily_change"][max(1, i - 4):i + 1]
    market = (market_stats or {}).get(bars[i]["trade_date"], {})
    market_amount = market.get("amount", 0.0)
    market_amount_ma20 = market.get("amount_ma20", 0.0)

    return {
        "corr_high_volume_10": metrics["high_vol_corr"][i],
        "vol_amp": metrics["vol_amp"][i],
        "near_high_pct": _safe_div(close, metrics["high_20"][i], 1.0) - 1,
        "chg_1d": metrics["daily_change"][i],
        "chg_3d": _ratio_from_n(closes, i, 3),
        "chg_5d": metrics["chg_5d"][i],
        "chg_10d": _ratio_from_n(closes, i, 10),
        "chg_20d": _ratio_from_n(closes, i, 20),
        "amount": amounts[i],
        "amount_ratio_20": _safe_div(amounts[i], amount_ma20, 1.0),
        "volume_ratio_20": _safe_div(volumes[i], volume_ma20, 1.0),
        "amplitude": _safe_div(day_range, close),
        "close_position": 1.0 if day_range <= 0 else (close - low) / day_range,
        "close_ma5_ratio": _safe_div(close, ma5, 1.0) - 1,
        "close_ma10_ratio": _safe_div(close, ma10, 1.0) - 1,
        "close_ma20_ratio": _safe_div(close, ma20, 1.0) - 1,
        "ma5_ma20_ratio": _safe_div(ma5, ma20, 1.0) - 1,
        "max_drawdown_5d": _max_drawdown(closes[max(0, i - 4):i + 1]),
        "up_days_5d": sum(1 for v in recent_changes if v > 0),
        "limit_up_recent_3d": int(alpha042._has_recent_limit_up(metrics["daily_change"], i, 3)),
        "market_breadth": market.get("breadth", 0.5),
        "market_above_ma20_ratio": market.get("above_ma20_ratio", 0.5),
        "market_false_breakout_rate_5d": market.get("false_breakout_rate_5d", 0.0),
        "market_ret_20d": market.get("market_ret_20d", 0.0),
        "market_range_20d": market.get("market_range_20d", 0.0),
        "market_amount_ratio": _safe_div(market_amount, market_amount_ma20, 1.0),
    }


def _score_buy(metrics, bars, i, market_stats):
    bundle = _load_model_bundle()
    model = bundle["model"]
    threshold = float(bundle.get("threshold", 0.5))
    features = _features_for(metrics, bars, i, market_stats)
    row = [[features.get(col) for col in FEATURE_COLUMNS]]
    probability = float(model.predict_proba(row)[0][1])
    return probability, threshold


def market_gate(date, market_stats):
    return alpha042.market_gate(date, market_stats)


def generate_signals(bars, market_stats=None):
    metrics = alpha042._compute_metrics(bars)
    closes = metrics["closes"]
    n = len(closes)

    signals = []
    in_pos = False
    entry_price = None
    entry_index = None

    min_idx = max(alpha042.VOL_LONG, alpha042.PRICE_NEAR_HIGH_LOOKBACK, alpha042.CORR_WINDOW) + 5
    raw_buy_indices = []
    feature_rows = []
    for i in range(min_idx, n):
        if not _is_raw_alpha042_buy(metrics, i):
            continue
        features = _features_for(metrics, bars, i, market_stats or {})
        raw_buy_indices.append(i)
        feature_rows.append([features.get(col) for col in FEATURE_COLUMNS])

    buy_prob_by_index = {}
    threshold = 0.5
    if feature_rows:
        bundle = _load_model_bundle()
        threshold = float(bundle.get("threshold", 0.5))
        probabilities = bundle["model"].predict_proba(feature_rows)[:, 1]
        buy_prob_by_index = {
            i: float(prob)
            for i, prob in zip(raw_buy_indices, probabilities)
        }

    for i in range(min_idx, n):
        close = closes[i]
        corr_val = metrics["high_vol_corr"][i]
        vol_amp = metrics["vol_amp"][i]
        high_20 = metrics["high_20"][i]
        chg_5d = metrics["chg_5d"][i]

        if corr_val is None or vol_amp is None or high_20 is None or chg_5d is None:
            continue

        if not in_pos:
            probability = buy_prob_by_index.get(i)
            if probability is None:
                continue
            if probability < threshold:
                continue

            in_pos = True
            entry_price = close
            entry_index = i
            signals.append({
                "date": bars[i]["trade_date"],
                "action": "buy",
                "reason": (
                    f"Alpha042 ML买点 prob={probability:.2f}>={threshold:.2f} | "
                    f"corr={corr_val:.2f} | 波动{vol_amp:.1f}x | "
                    f"距20日高{(close/high_20-1)*100:.1f}% | 5日{chg_5d*100:.1f}%"
                ),
            })
            continue

        if i == entry_index:
            continue

        hold_days = i - entry_index
        profit_pct = (close / entry_price - 1) * 100 if entry_price else 0
        reason = None

        if corr_val > alpha042.CORR_SELL_THRESH:
            reason = f"量价同步(散户涌入 corr={corr_val:.2f},盈{profit_pct:.1f}%)"
        elif hold_days in alpha042.RECHECK_HOLD_DAYS:
            price_broken = close < high_20 * (1 - alpha042.RECHECK_NEAR_HIGH_FAIL_PCT)
            weak_5d = chg_5d < alpha042.RECHECK_CHG_5D_FAIL
            corr_failed = corr_val > alpha042.RECHECK_CORR_FAIL
            if corr_failed or (price_broken and weak_5d):
                reason = (
                    f"{hold_days}日复查走弱("
                    f"corr={corr_val:.2f},距20日高{(close/high_20-1)*100:.1f}%,"
                    f"5日{chg_5d*100:.1f}%,盈{profit_pct:.1f}%)"
                )
        elif hold_days >= alpha042.MAX_HOLD_DAYS:
            reason = f"持仓{hold_days}天到期(盈{profit_pct:.1f}%)"

        if reason is None:
            continue

        signals.append({"date": bars[i]["trade_date"], "action": "sell", "reason": reason})
        in_pos = False
        entry_price = None
        entry_index = None

    return signals


if __name__ == "__main__":
    from backtest.strategy.runner import run_strategy_meta
    run_strategy_meta(META)
