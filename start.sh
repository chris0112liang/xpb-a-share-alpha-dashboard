#!/bin/bash
# 小彭博社 — 一键启动（推荐方式）

echo "=== 小彭博社 启动中 ==="
echo ""

if supervisorctl -c /etc/supervisor/supervisord.conf status > /dev/null 2>&1; then
  echo "supervisor 已在运行，启动进程..."
  supervisorctl -c /etc/supervisor/supervisord.conf start all 2>&1
else
  echo "启动 supervisor 守护进程..."
  tmux new-session -d -s xpb-supervisor 'supervisord -c /etc/supervisor/supervisord.conf -n'
  # 等待服务启动
  sleep 3
fi

# 等待服务就绪
echo "等待服务就绪..."
for i in $(seq 1 20); do
  sleep 3
  bc=$([ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/ 2>/dev/null)" = "404" ] && echo "200" || echo "000")
  fc=$([ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5173/ 2>/dev/null)" = "200" ] && echo "200" || echo "000")
  status="backend=$([ "$bc" = "200" ] && echo "✅" || echo "⏳") frontend=$([ "$fc" = "200" ] && echo "✅" || echo "⏳")"
  echo "  ${i}s → $status"
  [ "$bc" = "200" ] && [ "$fc" = "200" ] && { echo ""; break; }
done

echo "========================================="
echo "  ✅ 小彭博社 启动完成！"
echo "  📎 打开: http://localhost:5173/"
echo "  📋 状态: supervisorctl status"
echo "========================================="
