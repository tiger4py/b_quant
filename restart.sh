#!/bin/bash
echo "=== b_quant 重启 ==="

# 杀掉占用8000端口的进程
PID=$(lsof -ti:8000 2>/dev/null)
if [ -n "$PID" ]; then
    echo "关闭进程: $PID"
    kill -9 $PID
    sleep 2
fi

# 启动
cd "$(dirname "$0")"
echo "启动服务..."
nohup python main.py > /dev/null 2>&1 &
echo "已启动 PID: $!"
