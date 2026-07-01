#!/bin/bash
# ============================================================
# 本地早晨同步脚本：拉取服务器夜间推送的数据 + 导入本地库
#
# 用法:
#   bash script/daily_sync.sh              # 同步 + 导入 + 显示报告
#   bash script/daily_sync.sh --no-guide   # 只同步 + 导入，不显示报告
#   bash script/daily_sync.sh --restart    # 同步后重启 Web 服务
# ============================================================
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RESTART=false
NO_GUIDE=false
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=true ;;
        --no-guide) NO_GUIDE=true ;;
    esac
done

echo "=========================================="
echo "  b_quant 早晨数据同步"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# ======== Step 1: 拉取服务器夜间提交 ========
echo ""
echo "--- [1/3] git pull 拉取最新数据和策略结果 ---"
git pull
echo "  ✓ 代码+数据已更新"

# ======== Step 2: 导入最新CSV到本地数据库 ========
echo ""
echo "--- [2/3] 导入最新数据到本地数据库 ---"
python script/import_day_stock.py --type stock
python script/import_day_stock.py --type concept
echo "  ✓ 数据库已更新"

# ======== Step 3: 显示每日报告 ========
if [ "$NO_GUIDE" = false ]; then
    echo ""
    echo "--- [3/3] 每日指导报告 ---"
    python script/daily_guide.py --top 10
else
    echo ""
    echo "--- [3/3] 跳过报告 (--no-guide) ---"
fi

# ======== 可选: 重启 Web 服务 ========
if [ "$RESTART" = true ]; then
    echo ""
    echo "--- 重启 Web 服务 ---"
    PID=$(lsof -ti:8000 2>/dev/null || true)
    if [ -n "$PID" ]; then
        echo "  关闭旧进程: $PID"
        kill -9 $PID 2>/dev/null || true
        sleep 2
    fi
    # Windows 兼容: 也用 taskkill 试一次
    taskkill //F //IM python.exe //FI "WINDOWTITLE eq *main.py*" 2>/dev/null || true
    nohup python main.py > logs/main.log 2>&1 &
    echo "  ✓ 服务已启动 PID: $!"
fi

echo ""
echo "=========================================="
echo "  同步完成 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
