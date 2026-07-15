# -*- coding: utf-8 -*-
"""Train an sklearn filter for Alpha042 buy candidates.

Each Alpha042 raw buy condition becomes one sample. The label is whether the
future N-day high reaches a target return from the buy close.
"""

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    precision_recall_curve,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline

from backtest.portfolio import _build_market_stats
from backtest.strategy import strategy_alpha042 as alpha042
from script.run_backtest import load_stock_bars


OUTPUT_DIR = ROOT_DIR / "data" / "ml" / "alpha042_buy_filter"

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
    market = market_stats.get(bars[i]["trade_date"], {})
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


def build_samples(bars_by_code, stock_map, horizon_days, target_return):
    market_stats = _build_market_stats(bars_by_code)
    samples = []

    for code, bars in sorted(bars_by_code.items()):
        clean_bars = [b for b in bars if b.get("close") and b.get("open")]
        if len(clean_bars) < max(alpha042.VOL_LONG + 10, horizon_days + 80):
            continue

        metrics = alpha042._compute_metrics(clean_bars)
        min_idx = max(alpha042.VOL_LONG, alpha042.PRICE_NEAR_HIGH_LOOKBACK, alpha042.CORR_WINDOW) + 5
        for i in range(min_idx, len(clean_bars) - horizon_days):
            if not _is_raw_alpha042_buy(metrics, i):
                continue

            close = metrics["closes"][i]
            future = clean_bars[i + 1:i + 1 + horizon_days]
            future_high = max(float(b["high"]) for b in future)
            future_low = min(float(b["low"]) for b in future)
            future_max_return = future_high / close - 1 if close else 0.0
            future_max_drawdown = future_low / close - 1 if close else 0.0
            label = int(future_max_return >= target_return)
            features = _features_for(metrics, clean_bars, i, market_stats)

            row = {
                "date": clean_bars[i]["trade_date"],
                "code": code,
                "name": stock_map.get(code, {}).get("name", code),
                "close": close,
                "future_max_return": future_max_return,
                "future_max_drawdown": future_max_drawdown,
                "label": label,
            }
            row.update(features)
            samples.append(row)

    samples.sort(key=lambda x: (x["date"], x["code"]))
    return samples


def _matrix(rows):
    return [[row.get(col) for col in FEATURE_COLUMNS] for row in rows]


def _labels(rows):
    return [int(row["label"]) for row in rows]


def _best_threshold(y_true, probs, min_precision=0.55):
    precision, recall, thresholds = precision_recall_curve(y_true, probs)
    best = {"threshold": 0.5, "precision": 0.0, "recall": 0.0, "score": -1.0}
    for p, r, t in zip(precision[:-1], recall[:-1], thresholds):
        if p < min_precision:
            continue
        score = p * r
        if score > best["score"]:
            best = {"threshold": float(t), "precision": float(p), "recall": float(r), "score": float(score)}
    if best["score"] < 0:
        for p, r, t in zip(precision[:-1], recall[:-1], thresholds):
            score = p * r
            if score > best["score"]:
                best = {"threshold": float(t), "precision": float(p), "recall": float(r), "score": float(score)}
    return best


def _probability_bucket_report(rows, probs, bucket_size=0.1):
    buckets = {}
    for row, prob in zip(rows, probs):
        bucket = min(0.9, math.floor(prob / bucket_size) * bucket_size)
        key = f"{bucket:.1f}-{bucket + bucket_size:.1f}"
        item = buckets.setdefault(key, {"count": 0, "success": 0, "avg_future_max_return": 0.0})
        item["count"] += 1
        item["success"] += int(row["label"])
        item["avg_future_max_return"] += float(row["future_max_return"])
    for item in buckets.values():
        item["success_rate"] = round(item["success"] / item["count"], 4) if item["count"] else 0
        item["avg_future_max_return"] = round(item["avg_future_max_return"] / item["count"], 4) if item["count"] else 0
    return dict(sorted(buckets.items()))


def write_samples_csv(path, samples):
    if not samples:
        return
    columns = [
        "date", "code", "name", "close", "label",
        "future_max_return", "future_max_drawdown",
    ] + FEATURE_COLUMNS
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in samples:
            writer.writerow({col: row.get(col) for col in columns})


def train(args):
    bars_by_code, stock_map = load_stock_bars(start_date=args.start)
    samples = build_samples(
        bars_by_code=bars_by_code,
        stock_map=stock_map,
        horizon_days=args.horizon_days,
        target_return=args.target_return,
    )
    if not samples:
        raise RuntimeError("no Alpha042 buy samples found")

    train_rows = [row for row in samples if row["date"] <= args.train_end]
    test_rows = [row for row in samples if row["date"] > args.train_end]
    if len(train_rows) < 50 or len(test_rows) < 20:
        raise RuntimeError(f"not enough samples: train={len(train_rows)} test={len(test_rows)}")

    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("clf", RandomForestClassifier(
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            class_weight="balanced",
            random_state=args.random_state,
            n_jobs=-1,
        )),
    ])
    x_train = _matrix(train_rows)
    y_train = _labels(train_rows)
    x_test = _matrix(test_rows)
    y_test = _labels(test_rows)
    model.fit(x_train, y_train)

    train_probs = model.predict_proba(x_train)[:, 1]
    test_probs = model.predict_proba(x_test)[:, 1]
    threshold = _best_threshold(y_test, test_probs, min_precision=args.min_precision)
    y_pred = [int(p >= threshold["threshold"]) for p in test_probs]

    clf = model.named_steps["clf"]
    importances = [
        {"feature": feature, "importance": float(importance)}
        for feature, importance in sorted(
            zip(FEATURE_COLUMNS, clf.feature_importances_),
            key=lambda x: x[1],
            reverse=True,
        )
    ]

    report = {
        "strategy": "alpha042_buy_filter",
        "model": "RandomForestClassifier",
        "start": args.start,
        "train_end": args.train_end,
        "horizon_days": args.horizon_days,
        "target_return": args.target_return,
        "feature_columns": FEATURE_COLUMNS,
        "samples": {
            "total": len(samples),
            "train": len(train_rows),
            "test": len(test_rows),
            "train_positive_rate": round(sum(y_train) / len(y_train), 4),
            "test_positive_rate": round(sum(y_test) / len(y_test), 4),
        },
        "metrics": {
            "train_auc": round(roc_auc_score(y_train, train_probs), 4) if len(set(y_train)) > 1 else None,
            "test_auc": round(roc_auc_score(y_test, test_probs), 4) if len(set(y_test)) > 1 else None,
            "test_accuracy": round(accuracy_score(y_test, y_pred), 4),
            "confusion_matrix": confusion_matrix(y_test, y_pred).tolist(),
            "classification_report": classification_report(y_test, y_pred, output_dict=True, zero_division=0),
        },
        "selected_threshold": threshold,
        "probability_buckets": _probability_bucket_report(test_rows, test_probs),
        "feature_importances": importances,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": model,
        "feature_columns": FEATURE_COLUMNS,
        "threshold": threshold["threshold"],
        "report": report,
    }, OUTPUT_DIR / "model.pkl")
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_samples_csv(OUTPUT_DIR / "samples.csv", samples)
    return report


def main():
    parser = argparse.ArgumentParser(description="Train sklearn filter for Alpha042 buy points.")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--horizon-days", type=int, default=10)
    parser.add_argument("--target-return", type=float, default=0.08)
    parser.add_argument("--min-precision", type=float, default=0.55)
    parser.add_argument("--n-estimators", type=int, default=400)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--min-samples-leaf", type=int, default=8)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    report = train(args)
    metrics = report["metrics"]
    samples = report["samples"]
    threshold = report["selected_threshold"]
    print("Alpha042 buy filter trained")
    print(f"samples total={samples['total']} train={samples['train']} test={samples['test']}")
    print(f"positive_rate train={samples['train_positive_rate']:.2%} test={samples['test_positive_rate']:.2%}")
    print(f"test_auc={metrics['test_auc']} accuracy={metrics['test_accuracy']}")
    print(
        "threshold="
        f"{threshold['threshold']:.3f} precision={threshold['precision']:.3f} recall={threshold['recall']:.3f}"
    )
    print(f"saved: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
