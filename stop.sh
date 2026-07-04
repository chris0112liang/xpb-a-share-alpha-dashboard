#!/bin/bash
# 小彭博社 — 停止进程

echo "=== 停止小彭博社 ==="
echo ""

if supervisorctl -c /etc/supervisor/supervisord.conf status > /dev/null 2>&1; then
  supervisorctl -c /etc/supervisor/supervisord.conf stop all 2>&1
  echo "  ✅ 已停止"
else
  # 直接杀掉
  fuser -k 8000/tcp 2>/dev/null
  fuser -k 5173/tcp 2>/dev/null
  echo "  ✅ 已清理进程"
fi
