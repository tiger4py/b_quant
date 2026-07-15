#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily flow: update data, import CSV, then optionally push notifications."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(str(ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_step(name, *cmd):
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")
    result = subprocess.run([sys.executable] + list(cmd), cwd=str(ROOT))
    ok = result.returncode == 0
    print(f"  -> {'OK' if ok else f'FAIL (code={result.returncode})'}")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Daily data update and push flow")
    parser.add_argument("--no-push", action="store_true", help="Skip QQ push")
    parser.add_argument("--days", type=int, default=1000, help="Unused legacy option")
    parser.add_argument("--max-positions", type=int, default=5, help="Unused legacy option")
    args = parser.parse_args()

    run_step("Step 1/8: update stock K-line data", "script/update_base_data/update_stock.py")
    run_step("Step 2/8: update concept files", "script/update_base_data/update_concept_ths.py")
    run_step("Step 3/8: update ETF files", "script/update_base_data/update_etf.py")
    run_step("Step 4/8: import stock CSV into database", "script/update_base_data/import_day_stock.py", "--type", "stock", "-q")
    run_step("Step 5/8: run stock strategy backtests", "script/run_backtest.py", "--universe", "stock", "--strategy", "all")
    run_step("Step 6/8: run concept strategy backtests", "script/run_backtest.py", "--universe", "concept", "--strategy", "all")
    run_step("Step 7/8: run ETF strategy backtests", "script/run_backtest.py", "--universe", "etf", "--strategy", "all")

    if args.no_push:
        print(f"\n{'=' * 60}")
        print("  Step 8/8: skip push (--no-push)")
        print(f"{'=' * 60}")
    else:
        run_step("Step 8/8: QQ push", "script/push_latest_trades.py")

    print("\nDone.")


if __name__ == "__main__":
    main()
