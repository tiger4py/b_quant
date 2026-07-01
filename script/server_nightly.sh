#!/bin/bash
# ============================================================
# 服务器每晚11点自动任务
# 流程: 拉代码 → 拉数据 → 导入库 → 跑策略 → 生成报告 → 提交推送
#
# 用法:
#   bash script/server_nightly.sh              # 完整流程
#   bash script/server_nightly.sh --no-push    # 不推送（调试用）
#   bash script/server_nightly.sh --no-guide   # 不生成报告
#
# 配合 Windows 任务计划程序 / Linux crontab:
#   0 23 * * 1-5   bash /path/to/script/server_nightly.sh
# ============================================================
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ======== 参数 ========
NO_PUSH=false
NO_GUIDE=false
for arg in "$@"; do
    case "$arg" in
        --no-push) NO_PUSH=true ;;
        --no-guide) NO_GUIDE=true ;;
    esac
done

# ======== 日志 ========
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/nightly_$(date +%Y%m%d_%H%M%S).log"
# 同时输出到终端和日志文件
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  b_quant 夜间自动化任务"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  日志: $LOG_FILE"
echo "=========================================="

# ======== 工具函数 ========
run_step() {
    local step_name="$1"
    shift
    echo ""
    echo "--- [$step_name] $(date '+%H:%M:%S') ---"
    if "$@"; then
        echo "  ✓ $step_name 完成"
        return 0
    else
        local rc=$?
        echo "  ✗ $step_name 失败 (exit=$rc)"
        return $rc
    fi
}

# ======== Step 1: 拉取最新代码 ========
run_step "1/7: git pull 拉取代码" git pull || {
    echo "[WARN] git pull 失败，继续执行（可能有本地修改）"
}

# ======== Step 2: 拉取最新日K线数据 ========
run_step "2/7: 更新日K线 + 概念指数" python script/update_daily.py --type all || {
    echo "[WARN] update_daily 失败，检查网络/BaoStock状态"
}

# ======== Step 3: 导入CSV到数据库 ========
run_step "3/7: 导入stock CSV到数据库" python script/import_day_stock.py --type stock

# ======== Step 4: 导入概念数据 ========
run_step "4/7: 导入concept CSV到数据库" python script/import_day_stock.py --type concept

# ======== Step 5: 跑策略回测 ========
run_step "5/7: 大底抄底策略回测" python script/run_strategy_market_backtest.py \
    --strategy market_bottom \
    --days 1000 \
    --max-positions 5

# ======== Step 6: 生成每日指导报告 ========
if [ "$NO_GUIDE" = false ]; then
    run_step "6/7: 生成每日指导报告" python script/daily_guide.py
else
    echo ""
    echo "--- [6/7] 跳过 (--no-guide) ---"
fi

# ======== Step 7: 提交并推送 ========
if [ "$NO_PUSH" = false ]; then
    run_step "7/7: git commit & push" bash -c '
        git add data/day_stock/ data/day_concept/ data/*.txt data/*.json data/portfolio.json data/trade_history.json data/trade_log.json 2>/dev/null || true
        # stock.db 如果通过 LFS 追踪则强制添加
        git add -f data/stock.db 2>/dev/null || true
        git commit -m "chore: nightly auto update $(date +%Y-%m-%d)" 2>/dev/null && echo "  commit ok" || echo "  (无变更，跳过commit)"
        git push 2>/dev/null && echo "  push ok" || echo "  [WARN] push 失败，请检查网络"
    '
else
    echo ""
    echo "--- [7/7] 跳过推送 (--no-push) ---"
fi

# ======== 清理旧日志（保留最近30天） ========
find "$LOG_DIR" -name "nightly_*.log" -mtime +30 -delete 2>/dev/null || true

echo ""
echo "=========================================="
echo "  夜间任务完成 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
