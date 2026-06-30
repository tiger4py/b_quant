#!/bin/bash
# 每日同步脚本：拉代码 → 导数据 → 重启项目
# 用法: bash script/daily_sync.sh
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== [1/3] git pull ==="
git pull

echo "=== [2/3] 导入最新 CSV 到数据库 ==="
python script/import_day_stock.py

echo "=== [3/3] 重启项目 ==="
# 杀掉旧进程
taskkill //F //IM python.exe //FI "WINDOWTITLE eq *main.py*" 2>/dev/null || true
# 启动新进程
nohup python main.py > logs/main.log 2>&1 &

echo "=== 完成 ==="
