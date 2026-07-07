import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


def parse_factors(text):
    result = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start = int(left)
            end = int(right)
            if start > end:
                start, end = end, start
            result.extend(range(start, end + 1))
        else:
            result.append(int(part))
    seen = set()
    factors = []
    for n in result:
        if n < 1 or n > 191:
            raise ValueError(f"factor out of range: {n}")
        if n in seen:
            continue
        seen.add(n)
        factors.append(f"gtja_alpha{n:03d}")
    return factors


def main_cli():
    parser = argparse.ArgumentParser(description="Run GTJA Alpha factor lab for concept data.")
    parser.add_argument("--factors", required=True, help="Factor numbers, e.g. 001 or 001-010 or 001,005,042")
    parser.add_argument("--start-date", default="2022-01-15", help="Backtest start date, default 2022-01-15")
    parser.add_argument("--end-date", required=True, help="Backtest end date, e.g. 2026-07-07")
    parser.add_argument("--top-k", type=int, default=5, help="Top K holdings, default 5")
    parser.add_argument("--source", default="concept", choices=["concept", "etf"], help="Data source, default concept")
    args = parser.parse_args()

    factors = parse_factors(args.factors)
    invalid = [f for f in factors if f not in main.FACTOR_META]
    if invalid:
        print("These factors are not enabled in current Lab:", ", ".join(invalid))
        print("Currently enabled:", ", ".join(main.FACTOR_META.keys()))
        return 2

    payload = {
        "source": args.source,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "top_k": args.top_k,
        "factors": factors,
    }

    with main.app.test_client() as client:
        resp = client.post("/api/lab/backtest", json=payload)
        data = resp.get_json()

    print("status:", resp.status_code)
    if not data:
        print("no json response")
        return 1
    if data.get("error"):
        print("error:", data["error"])
        return 1

    main._save_lab_factor_results(data, args.end_date)

    print("window:", data.get("window"))
    print("requested:", len(data.get("requested_factors", [])), data.get("requested_factors", []))
    print("available:", len(data.get("available_factors", [])), data.get("available_factors", []))
    print("cache:", data.get("cache", {}).get("key"))
    for factor in factors:
        fdata = (data.get("factors") or {}).get(factor)
        if not fdata:
            print(f"{factor}: no result")
            continue
        stats = fdata.get("stats", {})
        print(
            f"{factor}: return={stats.get('total_return')}%, "
            f"sharpe={stats.get('sharpe')}, "
            f"max_dd={stats.get('max_dd')}%, "
            f"trades={stats.get('trade_count')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
