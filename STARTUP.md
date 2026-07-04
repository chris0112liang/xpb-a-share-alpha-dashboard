# 小彭博社 — 快速启动指南

## 启动

```bash
bash /mnt/e/claude-code-rev/start.sh
```

浏览器打开 `http://localhost:5173/`

## 单独操作

### 后端 (Python / FastAPI)

```bash
cd /mnt/e/claude-code-rev/backend
source ../.venv/bin/activate
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/backend.log 2>&1 &
```

### 前端 (Vite / React)

```bash
cd /mnt/e/claude-code-rev
rm -rf node_modules/.vite
nohup npx vite --host 0.0.0.0 --port 5173 --force > /tmp/frontend.log 2>&1 &
```

### 进程管理

```bash
# 查看是否在运行
curl -s http://localhost:8000/api/hot/news | head -c 100   # 后端
curl -s -o /dev/null -w "%{http_code}" http://localhost:5173/   # 前端

# 查看日志
tail -f /tmp/backend.log
tail -f /tmp/frontend.log

# 杀进程
pkill -f "python3 -m uvicorn"
pkill -f "node.*vite"
```

## 目录结构

```
/mnt/e/claude-code-rev/
├── index.html          # 入口页面
├── src/                # 前端 React 源码
│   ├── App.tsx         # 主组件（搜索、图表、面板）
│   ├── index.css       # 样式
│   └── components/     # 子组件
├── backend/            # 后端 Python 源码
│   ├── main.py         # FastAPI 路由
│   ├── alpha/          # Alpha 策略引擎
│   └── ...
├── .venv/              # Python 虚拟环境
├── start.sh            # 一键启动脚本
└── STARTUP.md          # 本文件
```

## 注意事项

- 前端改了代码后，如果热更新不生效，**手动刷新浏览器** (F5)
- WSL2 环境变动不大时不需要清 `.vite` 缓存，只有修改了 .tsx 文件结构时需要
- 后端改了 Python 文件后需要**重启进程**才能生效
- 百度热搜和财信快讯每次请求实时拉取（无缓存）
- 指数行情：交易日 9:30-15:00 每 60 秒刷新，盘后冻结
