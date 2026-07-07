import math

import numpy as np
import pandas as pd

from logic.vendor.gtja_alpha191_raw import GTJA_191


def _install_pandas_legacy_rolling():
    if not hasattr(pd, "rolling_mean"):
        pd.rolling_mean = lambda obj, window, *args, **kwargs: obj.rolling(window).mean()
    if not hasattr(pd, "rolling_sum"):
        pd.rolling_sum = lambda obj, window, *args, **kwargs: obj.rolling(window).sum()
    if not hasattr(pd, "rolling_std"):
        pd.rolling_std = lambda obj, window, *args, **kwargs: obj.rolling(window).std()
    if not hasattr(pd, "rolling_min"):
        pd.rolling_min = lambda obj, window, *args, **kwargs: obj.rolling(window).min()
    if not hasattr(pd, "rolling_max"):
        pd.rolling_max = lambda obj, window, *args, **kwargs: obj.rolling(window).max()
    if not hasattr(pd, "rolling_corr"):
        pd.rolling_corr = lambda a, b, window, *args, **kwargs: a.rolling(window).corr(b)
    if not hasattr(pd, "rolling_cov"):
        pd.rolling_cov = lambda a, b, window, *args, **kwargs: a.rolling(window).cov(b)
    if not hasattr(pd, "rolling_apply"):
        pd.rolling_apply = lambda obj, window, func, *args, **kwargs: obj.rolling(window).apply(func, raw=False)
    if not hasattr(pd, "ewma"):
        def _ewma(obj, alpha=None, span=None, com=None, adjust=False, *args, **kwargs):
            if alpha is not None:
                return obj.ewm(alpha=alpha, adjust=adjust).mean()
            if span is not None:
                return obj.ewm(span=span, adjust=adjust).mean()
            if com is not None:
                return obj.ewm(com=com, adjust=adjust).mean()
            return obj.ewm(adjust=adjust).mean()
        pd.ewma = _ewma


_install_pandas_legacy_rolling()


GTJA_ALPHA_ALL = [f"gtja_alpha{i:03d}" for i in range(1, 192)]
GTJA_ALPHA_PHASE1 = [f"gtja_alpha{i:03d}" for i in range(1, 51)]
GTJA_ALPHA_UNIMPLEMENTED_RAW = {27, 30, 50, 51, 55, 69, 73, 121, 131, 143, 151, 165, 166, 181, 183, 190}
GTJA_ALPHA_PHASE1_ACTIVE = [f"gtja_alpha{i:03d}" for i in range(1, 51) if i not in GTJA_ALPHA_UNIMPLEMENTED_RAW]


def gtja_alpha_name(name):
    if name.startswith("gtja_alpha"):
        return name
    if name.startswith("alpha"):
        return f"gtja_alpha{int(name[5:]):03d}"
    return name


class GTJA191Frame(GTJA_191):
    def __init__(self, frames):
        self.open_price = frames["open"]
        self.close = frames["close"]
        self.low = frames["low"]
        self.high = frames["high"]
        self.avg_price = frames["vwap"]
        self.prev_close = self.close.shift()
        self.volume = frames["volume"]
        self.amount = frames["amount"]
        self.benchmark_open_price = self.open_price.mean(axis=1)
        self.benchmark_close_price = self.close.mean(axis=1)


def build_gtja_frames(items_data):
    all_dates = sorted({r["trade_date"] for rows in items_data.values() for r in rows})
    columns = sorted(items_data.keys())
    frames = {
        "open": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
        "high": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
        "low": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
        "close": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
        "volume": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
        "amount": pd.DataFrame(index=all_dates, columns=columns, dtype=float),
    }
    for code, rows in items_data.items():
        for r in rows:
            dt = r["trade_date"]
            frames["open"].at[dt, code] = r.get("open")
            frames["high"].at[dt, code] = r.get("high")
            frames["low"].at[dt, code] = r.get("low")
            frames["close"].at[dt, code] = r.get("close")
            frames["volume"].at[dt, code] = r.get("volume") or 0
            frames["amount"].at[dt, code] = r.get("amount") or 0
    fallback_vwap = (frames["high"] + frames["low"] + frames["close"]) / 3
    frames["vwap"] = frames["amount"].where(frames["volume"] > 0).div(frames["volume"].where(frames["volume"] > 0)).fillna(fallback_vwap)
    return frames


def _method_name(factor_name):
    n = int(factor_name.replace("gtja_alpha", ""))
    if n in (88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99):
        return f"alpha_{n}"
    return f"alpha_{n:03d}"


def compute_gtja_alpha_snapshot(frames, factor_names, end_idx):
    sub = {k: v.iloc[:end_idx + 1] for k, v in frames.items()}
    calc = GTJA191Frame(sub)
    values = {}
    for fn in factor_names:
        n = int(fn.replace("gtja_alpha", ""))
        if n in GTJA_ALPHA_UNIMPLEMENTED_RAW:
            values[fn] = pd.Series(dtype=float)
            continue
        method = getattr(calc, _method_name(fn), None)
        if method is None:
            values[fn] = pd.Series(dtype=float)
            continue
        try:
            s = method()
            if not isinstance(s, pd.Series):
                s = pd.Series(dtype=float)
            s = pd.to_numeric(s, errors="coerce")
            s = s.replace([np.inf, -np.inf], np.nan).dropna()
            values[fn] = s
        except Exception:
            values[fn] = pd.Series(dtype=float)
    return values


def precompute_gtja_factor_series(items_data, factor_names, start_date=None, lookback=260):
    frames = build_gtja_frames(items_data)
    dates = list(frames["close"].index)
    result = {fn: {code: [] for code in frames["close"].columns} for fn in factor_names}
    start_idx = max(lookback, 1)
    if start_date:
        for i, dt in enumerate(dates):
            if dt >= start_date:
                start_idx = max(start_idx, i)
                break
    for idx in range(start_idx, len(dates)):
        dt = dates[idx]
        snapshots = compute_gtja_alpha_snapshot(frames, factor_names, idx)
        closes = frames["close"].iloc[idx]
        for fn, series in snapshots.items():
            for code, value in series.items():
                close = closes.get(code)
                if value is None or not np.isfinite(value) or close is None or not np.isfinite(close):
                    continue
                result[fn][code].append({"date": dt, "value": float(value), "close": float(close)})
    return {
        fn: {code: series for code, series in code_map.items() if series}
        for fn, code_map in result.items()
    }
