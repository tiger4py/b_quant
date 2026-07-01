#!/bin/bash
# ============================================================
# Linux crontab 配置脚本（适用于 Linux 服务器）
# 用法: bash script/setup_crontab.sh
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NIGHTLY_SCRIPT="$ROOT/script/server_nightly.sh"
CRON_LINE="0 23 * * 1-5  bash $NIGHTLY_SCRIPT >> $ROOT/logs/cron.log 2>&1"

echo "============================================"
echo "  b_quant 夜间 crontab 配置"
echo "============================================"
echo "  项目目录: $ROOT"
echo "  脚本:     $NIGHTLY_SCRIPT"
echo "  cron:     $CRON_LINE"
echo "============================================"

# 检查脚本存在
if [ ! -f "$NIGHTLY_SCRIPT" ]; then
    echo "[ERROR] 脚本不存在: $NIGHTLY_SCRIPT"
    exit 1
fi

# 确保日志目录
mkdir -p "$ROOT/logs"

# 追加到 crontab（去重）
if crontab -l 2>/dev/null | grep -qF "$NIGHTLY_SCRIPT"; then
    echo "  crontab 已存在，跳过"
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "  [OK] crontab 已添加"
fi

echo ""
echo "  查看: crontab -l"
echo "  手动运行: bash $NIGHTLY_SCRIPT"
echo "  日志: $ROOT/logs/nightly_*.log"
echo "============================================"
