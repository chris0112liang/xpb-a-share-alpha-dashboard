#!/bin/bash
# 清理 Vite 缓存并重启前端
echo "清理 Vite 缓存..."
rm -rf /mnt/e/claude-code-rev/node_modules/.vite
echo "重启前端..."
supervisorctl -c /etc/supervisor/supervisord.conf restart xiaopengboshi_frontend
echo "等待 8 秒..."
sleep 8
curl -s -o /dev/null -w "Frontend: %{http_code}\n" http://localhost:5173/
echo "OK"
