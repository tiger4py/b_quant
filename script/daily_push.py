#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动推送 — 数据更新 + 交易计划 + QQ推送

用法:
  python script/daily_push.py

定时任务（每天 8:30）:
  /cron "0 30 8 * * 1-5" "python script/daily_push.py"
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
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"[!] {name} 失败 (code={result.returncode})")
        return False
    return True


def main():
    # Step 1: 更新数据
    if not run_step("Step 1/2: 更新数据", "script/update_daily.py"):
        print("[!] 数据更新失败，使用现有数据继续")

    # Step 2: 生成交易计划 + QQ推送
    run_step("Step 2/2: 交易计划", "script/daily_trade.py")


if __name__ == "__main__":
    main()
