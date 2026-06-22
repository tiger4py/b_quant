#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日全流程：更新数据 → 回测 → 推送

用法:
  python script/daily_full_flow.py              # 完整流程 + QQ推送
  python script/daily_full_flow.py --no-push    # 不推送，仅输出
"""
import sys
import os
import subprocess
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
    result = subprocess.run(
        [sys.executable] + list(cmd),
        cwd=str(ROOT),
    )
    ok = result.returncode == 0
    print(f"  → {'OK' if ok else f'FAIL (code={result.returncode})'}")
    return ok


def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日全流程")
    parser.add_argument("--no-push", action="store_true", help="不推送QQ")
    parser.add_argument("--days", type=int, default=1000, help="回测天数")
    parser.add_argument("--max-positions", type=int, default=5, help="最大持仓")
    args = parser.parse_args()

    # Step 1: 更新数据
    run_step(f"Step 1/3: 更新K线数据",
             "script/update_daily.py")

    # Step 2: 趋势跟随回测
    if not run_step(f"Step 2/3: 趋势跟随回测 ({args.days}天)",
                    "script/run_strategy_market_backtest.py",
                    "--strategy", "trend_following",
                    "--days", str(args.days),
                    "--max-positions", str(args.max_positions)):
        print("[!] 回测失败，无法推送")
        return

    # Step 3: 推送
    if args.no_push:
        print(f"\n{'=' * 60}")
        print(f"  Step 3/3: 跳过推送 (--no-push)")
        print(f"{'=' * 60}")
    else:
        run_step(f"Step 3/3: QQ推送",
                 "script/push_latest_trades.py")

    print("\nDone.")


if __name__ == "__main__":
    main()
