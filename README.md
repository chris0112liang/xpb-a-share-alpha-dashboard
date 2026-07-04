# 小彭博社 A 股 Alpha 看板

一个前后端分离的 A 股市场分析项目。前端使用 Vite + React + TypeScript，后端使用 FastAPI，并通过 Akshare、Tencent 行情源、本地行业映射和磁盘缓存组合出市场状态、板块生命周期、热点新闻、个股分析和 Alpha 候选池。

> 项目用于投研看板和工程能力展示，不构成投资建议。

## 核心能力

- 市场状态：风险偏好、量能、涨跌家数、指数行情、风格和主线提示。
- 板块生命周期：支持 startup / main_rise_1 / acceleration / high_divergence / ice_recovery / decay 六阶段识别。
- Alpha Terminal：集中展示行情数据源状态、策略状态、生命周期覆盖、强势板块、事件预警和候选池风控原因。
- 策略选股工作台：基于板块生命周期筛选可操作方向，展示板块评分、风险标签、映射股票池样本和真实 Alpha 候选。
- Alpha 候选池：基于市场环境、板块周期、相对强度、流动性和风险收益比生成候选。
- 个股分析：K 线、均线、MACD、成交量、资金流、赔率和规则/AI 分析摘要。
- 热点与新闻：热点股票和新闻快讯独立加载，避免慢接口拖住首页。
- 风控保护：无真实价格或成交额时，候选池物理截断为空，不使用全 0 缓存生成推荐。
- 数据透明：模拟 K 线只用于空状态占位展示，界面明确标识为模拟数据，不进入评分、推荐或筛选逻辑。

## 技术栈

- Frontend: React 19, Vite, TypeScript, ECharts, Tailwind CSS
- Backend: FastAPI, Uvicorn, Akshare, Pandas, NumPy, DuckDB, PyArrow
- Data: 行情接口、本地行业映射 JSON、磁盘缓存

## 本地运行

后端：

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

前端：

```bash
npm install --include=dev
npm run dev
```

默认前端开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`。如果后端端口不同，可以设置：

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:18001 npm run dev
```

静态预览或部署时可以设置：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run build
npm run preview
```

前端支持模块直达：

- `/?view=dashboard`
- `/?view=terminal`
- `/?view=chart`
- `/?view=screener`

## 关键接口

- `GET /api/health`
- `GET /api/index/quotes`
- `GET /api/index/market-summary`
- `GET /api/market-state`
- `GET /api/market-full`
- `GET /api/hot/news`
- `GET /api/hot/newsflash`
- `GET /api/alpha/candidates`
- `GET /api/alpha/terminal`
- `GET /api/stock/600519`

## 数据边界

- 行业归属包含本地 JSON、注册表映射和关键词映射，需要结合公告、主营业务和交易所披露信息人工复核。
- 缓存数据会标记来源，不能等同于实时行情。
- 指数或行情源不可用时，接口返回不可用状态，不使用 0 价格冒充真实行情。
- 旧版 `/api/alpha/screener` 保留为兼容端点，不再触发慢速逐股扫描；前端使用 `/api/alpha/candidates`。

## 构建验证

```bash
npm run build
python -m py_compile backend/alpha_engine.py backend/main.py backend/alpha/scanner.py backend/market_cache.py backend/routes_alpha_os.py
```

## About

A 股智能投研系统：支持行情分析、板块生命周期建模与 Alpha 策略筛选
