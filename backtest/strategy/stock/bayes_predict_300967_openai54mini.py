# -*- coding: utf-8 -*-
"""
Multi-horizon Bayesian accuracy summary for sz.300967.

Evaluates horizons:
1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21 trading days.

Accuracy rule:
  - actual return > +1% => UP
  - actual return < -1% => DOWN
  - otherwise excluded from accuracy

This wrapper reuses the feature matrix from `300967_glm5.2.py`, which already
contains the same stock-specific feature engineering and Bayesian ensemble
implementation used in the current repository.

Run:
  python backtest/strategy/stock/bayes_predict_300967_openai54mini.py
"""

from __future__ import annotations

import contextlib
import io
import runpy
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_selection import mutual_info_classif
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
from sklearn.naive_bayes import BernoulliNB, GaussianNB
from sklearn.preprocessing import StandardScaler


ROOT_DIR = Path(__file__).resolve().parents[3]
SOURCE_SCRIPT = Path(__file__).with_name("300967_glm5.2.py")
OUTPUT_PATH = Path(__file__).with_suffix(".txt")
HORIZONS = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21]
WINDOWS = (250, 400, 600)
TOP_K = 15
NEUTRAL_BAND = 0.01


def load_source_globals() -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return runpy.run_path(str(SOURCE_SCRIPT), run_name="not_main")


def build_horizon_data(fe: pd.DataFrame, df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    tmp = fe.copy()
    future_close = df["close"].shift(-horizon)
    actual_chg = future_close / df["close"] - 1.0
    label = pd.Series(np.nan, index=df.index, dtype=float)
    label.loc[actual_chg > NEUTRAL_BAND] = 1.0
    label.loc[actual_chg < -NEUTRAL_BAND] = 0.0
    tmp["label"] = label
    tmp["actual_chg"] = actual_chg
    return tmp.dropna(subset=["label"]).reset_index(drop=True)


def select_features(data: pd.DataFrame, all_feats: list[str]) -> list[str]:
    x = data[all_feats].ffill().bfill().values
    y = data["label"].astype(int).values
    mi = mutual_info_classif(x, y, random_state=42)
    idx = np.argsort(mi)[-min(TOP_K, len(mi)) :]
    return [all_feats[i] for i in idx]


def walk_forward_probs(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    n = len(x)
    min_w = min(WINDOWS)
    raw = np.full(n - min_w, np.nan)

    for i in range(min_w, n):
        x_test = x[i : i + 1]
        probs = []
        for w in WINDOWS:
            if i - w < 0:
                continue
            x_tr_raw = x[i - w : i]
            y_tr = y[i - w : i]
            sc = StandardScaler()
            x_tr = sc.fit_transform(x_tr_raw)
            x_te = sc.transform(x_test)

            gnb = GaussianNB(var_smoothing=1e-8).fit(x_tr, y_tr)
            bnb = BernoulliNB(alpha=0.5).fit(x_tr, y_tr)
            probs.append((gnb.predict_proba(x_te)[0, 1] + bnb.predict_proba(x_te)[0, 1]) / 2.0)
        raw[i - min_w] = float(np.mean(probs))
    return raw


def metrics_for_horizon(fe: pd.DataFrame, df: pd.DataFrame, all_feats: list[str], horizon: int) -> dict:
    data = build_horizon_data(fe, df, horizon)
    selected = select_features(data, all_feats)
    x = data[selected].ffill().bfill().values
    y = data["label"].astype(int).values
    actual = data["actual_chg"].values

    raw = walk_forward_probs(x, y)
    y_true = y[min(WINDOWS) :]
    actual_test = actual[min(WINDOWS) :]

    split = int(len(raw) * 0.6)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.02, y_max=0.98)
    iso.fit(raw[:split], y_true[:split])
    cal = iso.predict(raw)
    preds = (cal > 0.5).astype(int)

    valid = np.abs(actual_test) >= NEUTRAL_BAND
    acc_valid = accuracy_score(y_true[valid], preds[valid]) if valid.any() else float("nan")
    acc_all = accuracy_score(y_true, preds)
    try:
        auc_valid = roc_auc_score(y_true[valid], cal[valid]) if valid.any() else float("nan")
    except Exception:
        auc_valid = float("nan")
    try:
        auc_all = roc_auc_score(y_true, cal)
    except Exception:
        auc_all = float("nan")

    brier = brier_score_loss(y_true, cal)
    return {
        "horizon": horizon,
        "selected_feats": selected,
        "acc_valid": float(acc_valid),
        "acc_all": float(acc_all),
        "auc_valid": float(auc_valid),
        "auc_all": float(auc_all),
        "brier": float(brier),
        "n_all": int(len(y_true)),
        "n_valid": int(valid.sum()),
        "n_excluded": int((~valid).sum()),
    }


def main() -> None:
    mod = load_source_globals()
    fe = mod["fe"].copy()
    df = mod["df"].reset_index(drop=True)
    all_feats = mod["all_feats"]

    rows = []
    lines = []
    lines.append("=" * 90)
    lines.append("sz.300967 multi-horizon Bayesian accuracy summary")
    lines.append(f"latest_trade_date: {df['trade_date'].iloc[-1].date()}")
    lines.append("neutral_band_for_accuracy: ±1.00%")
    lines.append("=" * 90)

    for h in HORIZONS:
        r = metrics_for_horizon(fe, df, all_feats, h)
        rows.append(r)
        lines.append(
            f"{h:>2}d  acc_valid={r['acc_valid']:.2%}  acc_all={r['acc_all']:.2%}  "
            f"auc_valid={r['auc_valid']:.4f}  brier={r['brier']:.4f}  "
            f"n_valid={r['n_valid']}  n_excluded={r['n_excluded']}"
        )

    best = max(rows, key=lambda r: (r["acc_valid"], r["auc_valid"], -r["brier"]))
    lines.append("-" * 90)
    lines.append(
        f"best_horizon={best['horizon']}d  best_acc_valid={best['acc_valid']:.2%}  "
        f"best_auc_valid={best['auc_valid']:.4f}  best_brier={best['brier']:.4f}"
    )
    lines.append("=" * 90)

    report = "\n".join(lines)
    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nreport_written_to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
