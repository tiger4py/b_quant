#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build mainstream ETF base JSON files under data/etf/base.

Outputs:
- data/etf/base/summary.json
- data/etf/base/etfs.json
- data/etf/base/by_etf/{code}.json
"""

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
ETF_DIR = DATA_DIR / "etf"
BASE_DIR = ETF_DIR / "base"
BY_ETF_DIR = BASE_DIR / "by_etf"
MAIN_CODES_PATH = DATA_DIR / "etf_codes_main.json"


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_main_etfs():
    items = read_json(MAIN_CODES_PATH)
    etfs = {}
    for item in items:
        code = (item.get("code") or "").strip()
        name = (item.get("name") or "").strip()
        if code:
            etfs[code] = {
                "code": code,
                "raw_code": code[2:] if code[:2] in ("sh", "sz", "bj") else code,
                "exchange": code[:2] if code[:2] in ("sh", "sz", "bj") else "",
                "name": name,
                "is_mainstream": True,
                "source": "data/etf_codes_main.json",
            }
    return etfs


def load_daily_stats(main_codes):
    stats = {
        code: {
            "first_trade_date": None,
            "latest_trade_date": None,
            "bar_count": 0,
            "latest_bar": None,
        }
        for code in main_codes
    }

    for path in sorted(ETF_DIR.glob("*/*.csv")):
        if not path.parent.name.isdigit():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("code") or "").strip()
                if code not in stats:
                    continue
                trade_date = (row.get("trade_date") or "").strip()
                if not trade_date:
                    continue

                item = stats[code]
                item["bar_count"] += 1
                if item["first_trade_date"] is None or trade_date < item["first_trade_date"]:
                    item["first_trade_date"] = trade_date
                if item["latest_trade_date"] is None or trade_date >= item["latest_trade_date"]:
                    item["latest_trade_date"] = trade_date
                    item["latest_bar"] = {
                        "trade_date": trade_date,
                        "open": _to_float(row.get("open")),
                        "high": _to_float(row.get("high")),
                        "low": _to_float(row.get("low")),
                        "close": _to_float(row.get("close")),
                        "volume": _to_int(row.get("volume")),
                        "amount": _to_float(row.get("amount")),
                    }
    return stats


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def build(refresh=True):
    etfs = load_main_etfs()
    stats = load_daily_stats(etfs.keys())

    if refresh and BY_ETF_DIR.exists():
        shutil.rmtree(BY_ETF_DIR)
    BY_ETF_DIR.mkdir(parents=True, exist_ok=True)

    result = []
    for code in sorted(etfs):
        item = {**etfs[code], **stats.get(code, {})}
        result.append(item)
        write_json(BY_ETF_DIR / f"{code}.json", item)

    with_daily = sum(1 for item in result if item.get("bar_count", 0) > 0)
    latest_dates = [item["latest_trade_date"] for item in result if item.get("latest_trade_date")]
    first_dates = [item["first_trade_date"] for item in result if item.get("first_trade_date")]
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "data/etf_codes_main.json + data/etf/YYYY/YYYY-MM.csv",
        "etf_count": len(result),
        "etf_with_daily": with_daily,
        "etf_without_daily": len(result) - with_daily,
        "first_trade_date": min(first_dates) if first_dates else None,
        "latest_trade_date": max(latest_dates) if latest_dates else None,
        "total_daily_bars": sum(item.get("bar_count", 0) for item in result),
    }

    write_json(BASE_DIR / "etfs.json", result)
    write_json(BASE_DIR / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build(refresh=True)
