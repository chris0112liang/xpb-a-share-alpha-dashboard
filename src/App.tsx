import { useState, useRef, useContext, createContext, useEffect, useCallback } from "react";
import { Search, TrendingUp, TrendingDown, ChartArea, ListFilter, Monitor, Terminal } from "lucide-react";
import axios from "axios";
import StockChart, { generateMockData, type OHLCVItem } from "./components/StockChart";
import StrategyScreener from "./components/StrategyScreener";
import AlphaTerminal from "./components/AlphaTerminal";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || "";
if (apiBaseUrl) {
  axios.defaults.baseURL = apiBaseUrl;
}

const defaultMockData = generateMockData(250);

interface MoneyFlow {
  main_net: number; super_large_net: number; large_net: number;
  medium_net: number; small_net: number; main_pct: number;
}
interface TradePlan {
  direction: string; entry: number; stop_loss: number;
  target: number; supplement: number; detail: string;
}
interface StockResponse {
  code: string; name: string; price: number;
  change: number; changePercent: number;
  snapshot: { ma5: number|null; ma10: number|null; ma20: number|null; alignment: string; dif: number|null; dea: number|null; macd: number|null; };
  moneyFlow: MoneyFlow | null;
  odds: {
    upside_pct: number; downside_pct: number;
    odds_ratio: number; rating: string;
  } | null;
  aiAnalysis: {
    zhuangjia: string; duokong: string; plan: TradePlan;
    conclusion: string; support: number; resistance: number;
    causalAnalysis: Array<{cause: string; effect: string; implication: string}>;
  } | null;
  data: OHLCVItem[];
  weekly?: OHLCVItem[];
  monthly?: OHLCVItem[];
  behaviorTags?: { tag: string; confidence: number; source: string; desc: string }[];
}

// ── 市场状态类型 ──
interface MarketState {
  environment: "risk_on" | "risk_off" | "range_bound";
  label: string;
  risk_appetite: string;
  main_line: string;
  volume_trend: string;
  adjustment_factor: number;
  last_updated: string;
  etf_count?: number;
  breadth?: {
    total: number; up: number; down: number; flat: number;
    up_pct: number; down_pct: number;
    avg_change: number; total_amount_yi: number;
    data_available: boolean;
  };
  fund_flow?: { top_inflow: string[]; top_outflow: string[] };
}

interface SectorLifecycle {
  phase: string;
  confidence: number;
  bias: number;
  strength_score: number;
}

interface MarketAlert {
  sector: string;
  severity: "danger" | "warning" | "info" | "opportunity";
  message: string;
  action: string;
}

interface MarketFullResponse {
  environment: MarketState | null;
  lifecycles: { sectors: Record<string, SectorLifecycle>; last_updated: string } | null;
  alerts: MarketAlert[];
  last_updated: string;
}

const MarketContext = createContext<MarketFullResponse | null>(null);
type AppMode = "dashboard" | "terminal" | "chart" | "screener";

function getInitialMode(): AppMode {
  if (typeof window === "undefined") return "dashboard";
  const raw = new URLSearchParams(window.location.search).get("view") || window.location.hash.replace(/^#/, "");
  return raw === "terminal" || raw === "chart" || raw === "screener" || raw === "dashboard"
    ? raw
    : "dashboard";
}

// ── 直接取数，不拦截 ──
function g(obj: unknown, field: string, fallback = ""): string {
  if (!obj || typeof obj !== "object") return fallback;
  const v = (obj as Record<string, unknown>)[field];
  if (v === null || v === undefined || v === false || v === "") return fallback;
  return String(v);
}
function gn(obj: unknown, field: string, fallback = 0): number {
  if (!obj || typeof obj !== "object") return fallback;
  const v = (obj as Record<string, unknown>)[field];
  if (v === null || v === undefined || v === false) return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function gd(obj: unknown, field: string, fallback = "震荡"): string {
  const v = g(obj, field, fallback);
  if (v === "看多" || v === "看空" || v === "震荡") return v;
  return fallback;
}

function average(values: number[]): number | null {
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function lastAverage(values: number[], days: number): number | null {
  if (values.length < days) return null;
  return average(values.slice(-days));
}

function emaSeries(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period) return result;
  const k = 2 / (period + 1);
  let prev = average(values.slice(0, period))!;
  result[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i]! * k + prev * (1 - k);
    result[i] = prev;
  }
  return result;
}

function computeMacdPoint(values: number[]) {
  const fast = emaSeries(values, 12);
  const slow = emaSeries(values, 26);
  const dif = values.map((_, i) => fast[i] != null && slow[i] != null ? fast[i]! - slow[i]! : null);
  const valid = dif.filter((value): value is number => value != null);
  const deaValid = emaSeries(valid, 9);
  const dea: (number | null)[] = new Array(values.length).fill(null);
  let cursor = 0;
  dif.forEach((value, index) => {
    if (value == null) return;
    dea[index] = deaValid[cursor] ?? null;
    cursor += 1;
  });
  const i = values.length - 1;
  const prev = Math.max(0, i - 1);
  const macd = dif[i] != null && dea[i] != null ? (dif[i]! - dea[i]!) * 2 : null;
  const prevMacd = dif[prev] != null && dea[prev] != null ? (dif[prev]! - dea[prev]!) * 2 : null;
  return { dif: dif[i] ?? null, dea: dea[i] ?? null, macd, prevMacd };
}

// ── MarketHeader: 顶部全局状态条 ──
const MARKET_ENV_CONFIG: Record<string, {color: string; dot: string}> = {
  risk_on: { color: "#22c55e", dot: "🟢" },
  risk_off: { color: "#ef4444", dot: "🔴" },
  range_bound: { color: "#eab308", dot: "🟡" },
};
function MarketHeader() {
  const full = useContext(MarketContext);
  const state = full?.environment;
  if (!state) return null;
  const cfg = MARKET_ENV_CONFIG[state.environment] || { color: "#6b7280", dot: "⚪" };
  const b = state.breadth;
  return (
    <div className="flex items-center gap-4 py-1 text-[0.7rem] text-text-secondary border-b border-border-default mb-2 flex-wrap">
      <span><span style={{ color: cfg.color }}>{cfg.dot}</span> <b style={{ color: cfg.color }}>{state.label}</b></span>
      {b?.data_available && (
        <span>全市场 <b className="text-accent-red">{b.up}↑</b> <b className="text-accent-green">{b.down}↓</b> <b className="text-text-muted">{b.flat}→</b> (共{b.total}只)</span>
      )}
      <span>风格: <b className={ state.risk_appetite === "高" ? 'text-accent-green' : state.risk_appetite === "低" ? 'text-accent-red' : 'text-accent-amber' }>{state.risk_appetite}风险</b></span>
      <span>量能: {state.volume_trend}</span>
    </div>
  );
}

// ── MarketContext: 市场气候深度解析 ──
const MARKET_MAIN_LINE_COLORS: Record<string, string> = {
  "主线清晰": "#22c55e",
  "无明显主线": "#ef4444",
  "轮动加速": "#eab308",
  "情绪扩散": "#60a5fa",
};
function MarketContextPanel() {
  const full = useContext(MarketContext);
  const state = full?.environment;
  if (!state) return null;
  const cfg = MARKET_ENV_CONFIG[state.environment] || { color: "#6b7280", dot: "⚪" };
  const mlColor = (() => {
    for (const [k, v] of Object.entries(MARKET_MAIN_LINE_COLORS)) {
      if (state.main_line.includes(k)) return v;
    }
    return "#6b7280";
  })();
  const factor = state.adjustment_factor ?? 1.0;
  const factorColor = factor >= 1.1 ? "#22c55e" : factor <= 0.9 ? "#ef4444" : "#6b7280";
  const updatedAgo = (() => {
    if (!state.last_updated) return "未知";
    const diff = (Date.now() - new Date(state.last_updated).getTime()) / 1000 / 60;
    if (diff < 1) return "刚刚";
    if (diff < 60) return `${Math.round(diff)}分钟前`;
    return `${Math.round(diff / 60)}小时前`;
  })();
  const b = state.breadth;
  const ff = state.fund_flow;
  const breadthSourceLabel = b?.data_available
    ? `全市场 ${b.total} 只个股实时采样`
    : "全市场涨跌家数暂不可用";

  return (
    <div className="rounded-lg border border-border-default bg-bg-card p-3 mb-3 text-xs">
      <div className="flex justify-between items-center mb-2">
        <div className="text-xs font-semibold text-text-secondary">🌤 市场气候</div>
        <span className="text-[0.6rem] text-text-muted">同步于 {updatedAgo}</span>
      </div>
      {b?.data_available && (
        <div className="flex gap-4 mb-2 text-[0.8rem]">
          <span>📈 <b className="text-accent-red">{b.up}</b> 家上涨</span>
          <span>📉 <b className="text-accent-green">{b.down}</b> 家下跌</span>
          <span>➖ {b.flat} 家平盘</span>
          <span className="text-text-muted">共 {b.total} 只</span>
        </div>
      )}
      <div className="text-text-primary/90 leading-relaxed">
        <span>{cfg?.dot || "⚪"} <b style={{ color: cfg?.color || "#6b7280" }}>{state.label}</b> · 系数 <b style={{ color: factorColor }}>x{factor.toFixed(2)}</b></span>
      </div>
      <div className="text-text-secondary mt-1">
        主线: <b style={{ color: mlColor }}>{state.main_line}</b>
      </div>
      {ff && ff.top_inflow.length > 0 && (
        <div className="mt-1.5 text-[0.65rem]">
          <span className="text-accent-red">资金流入: {ff.top_inflow.join(" · ")}</span>
        </div>
      )}
      {ff && ff.top_outflow.length > 0 && (
        <div className="mt-0.5 text-[0.65rem]">
          <span className="text-accent-green">资金流出: {ff.top_outflow.join(" · ")}</span>
        </div>
      )}
      <div className="text-text-muted text-[0.6rem] mt-1.5">
        基于 {state.etf_count} 个核心ETF + {breadthSourceLabel}
      </div>
    </div>
  );
}

// ── 板块生命周期全景 ──
const PHASE_LABELS: Record<string, { label: string; color: string; bg: string }> = {
  // 旧 4 阶段
  initiation: { label: "启动期", color: "#22c55e", bg: "rgba(34,197,94,0.12)" },
  strengthening: { label: "强化期", color: "#eab308", bg: "rgba(234,179,8,0.12)" },
  divergence: { label: "分歧期", color: "#f97316", bg: "rgba(249,115,22,0.12)" },
  // 6 阶段
  startup: { label: "启动期", color: "#22c55e", bg: "rgba(34,197,94,0.12)" },
  main_rise_1: { label: "主升一期", color: "#eab308", bg: "rgba(234,179,8,0.12)" },
  acceleration: { label: "加速期", color: "#f59e0b", bg: "rgba(245,158,11,0.15)" },
  high_divergence: { label: "高位分歧", color: "#f97316", bg: "rgba(249,115,22,0.15)" },
  decay: { label: "退潮期", color: "#ef4444", bg: "rgba(239,68,68,0.15)" },
  ice_recovery: { label: "冰点修复", color: "#06b6d4", bg: "rgba(6,182,212,0.12)" },
  // 异常状态
  noise: { label: "横盘震荡", color: "#6b7280", bg: "rgba(107,114,128,0.1)" },
  unknown: { label: "未知", color: "#9ca3af", bg: "rgba(107,114,128,0.08)" },
  detect_failed: { label: "感知中断", color: "#dc2626", bg: "rgba(220,38,38,0.12)" },
};

// 阶段排序权重（值越小越靠前）
const PHASE_SORT_ORDER: Record<string, number> = {
  main_rise_1: 0, acceleration: 1, startup: 2,
  high_divergence: 3, ice_recovery: 4, decay: 5,
  unknown: 6, noise: 7, detect_failed: 8,
};

function SectorLifecyclePanel() {
  const full = useContext(MarketContext);
  const sectors = full?.lifecycles?.sectors;
  const alerts = full?.alerts || [];
  if (!sectors) return null;
  const sectorEntries = Object.entries(sectors)
    .filter(([k]) => k !== "沪深300")
    .sort(([, a], [, b]) => {
      const orderA = PHASE_SORT_ORDER[a?.phase || "unknown"] ?? 9;
      const orderB = PHASE_SORT_ORDER[b?.phase || "unknown"] ?? 9;
      if (orderA !== orderB) return orderA - orderB;
      return (b?.strength_score ?? 0) - (a?.strength_score ?? 0); // 同阶段按强度降序
    });

  return (
    <div className="rounded-lg border border-border-default bg-bg-card p-3 mb-3 text-xs">
      <div className="flex items-baseline justify-between mb-2">
        <span className="text-xs font-semibold text-text-secondary">🏭 板块生命周期</span>
        <span className="text-[0.55rem] text-text-muted">近120日K线 · 30min更新</span>
      </div>
      {sectorEntries.map(([name, info], si) => {
        const phase = (PHASE_LABELS[info?.phase || "unknown"] || PHASE_LABELS.noise)!;
        return (
          <div key={`${name}-${si}`} className="flex items-center justify-between py-1.5 border-b border-border-default/50 text-[0.7rem]">
            <div className="text-text-primary/90 min-w-16">{name}</div>
            <div className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: phase.color }} />
              <span style={{ color: phase.color, fontWeight: 600 }}>{phase.label}</span>
            </div>
            <div className="text-text-muted text-[0.6rem]">
              乖离{(() => { const b = (info as any).bias; return b != null ? `${b > 0 ? "+" : ""}${typeof b === 'number' ? b.toFixed(1) : b}%` : "—"; })()}
            </div>
            <div className="text-text-muted text-[0.6rem]" title="阶段识别置信度，不代表上涨概率">
              {info.confidence > 0 ? `${(info.confidence * 100).toFixed(0)}%` : "0%"}匹配
            </div>
          </div>
        );
      })}
      <div className="mt-2 text-[0.58rem] leading-relaxed text-text-muted">
        阶段为模型识别结果，基于板块近120日价格/量能；“匹配”不是胜率或确定预测。
      </div>
      {/* 预警 */}
      {alerts.filter(a => a.severity === "danger").length > 0 && (
        <div className="mt-2 pt-2 border-t border-border-default/50">
          <div className="text-[0.65rem] font-semibold text-accent-red mb-1">⚠️ 联动预警</div>
          {alerts.filter(a => a.severity === "danger").slice(0, 2).map((a, i) => (
            <div key={i} className="text-[0.65rem] text-red-300 mb-0.5">
              🔴 [{a.sector}] {a.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [activeMode, setActiveMode] = useState<AppMode>(getInitialMode);
  const [code, setCode] = useState("000001");
  const [chartPeriod, setChartPeriod] = useState<"day" | "week" | "month" | "fiveDay">("day");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stock, setStock] = useState<StockResponse | null>(null);
  const [marketFull, setMarketFull] = useState<MarketFullResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [indexQuotes, setIndexQuotes] = useState<{name:string; price:number | null; change_pct:number | null; data_available?: boolean}[] | null>(null);
  const [marketSummary, setMarketSummary] = useState<{total:number; rise:number; fall:number; flat:number} | null>(null);
  useEffect(() => {
    const syncModeFromUrl = () => setActiveMode(getInitialMode());
    window.addEventListener("popstate", syncModeFromUrl);
    return () => window.removeEventListener("popstate", syncModeFromUrl);
  }, []);

  // ── 全量数据并行拉取（首次立即 + 每60s轮询） ──
  const fetchAllData = useCallback(async () => {
    const results = await Promise.allSettled([
      axios.get("/api/market-full", { timeout: 15000 }),
      axios.get("/api/index/quotes", { timeout: 10000 }),
      axios.get("/api/index/market-summary", { timeout: 10000 }),
    ]);
    const [marketRes, indexRes, summaryRes] = results;
    if (marketRes.status === "fulfilled") {
      setMarketFull(marketRes.value.data as MarketFullResponse);
    }
    if (indexRes.status === "fulfilled" && indexRes.value.data?.quotes) {
      setIndexQuotes(indexRes.value.data.quotes);
    }
    if (summaryRes.status === "fulfilled" && summaryRes.value.data?.total > 0) {
      setMarketSummary(summaryRes.value.data);
    }
  }, []);
  useEffect(() => {
    fetchAllData();
    const interval = setInterval(() => fetchAllData(), 60000);
    return () => clearInterval(interval);
  }, [fetchAllData]);

  useEffect(() => {
    if (stock || loading || error) return;
    search();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 后端返回的首层
  const s = stock;
  const sCode = g(s, "code", "000001");
  const sName = g(s, "name", "未知");
  const sPrice = gn(s, "price");
  const sChange = gn(s, "change");
  const sChangePct = gn(s, "changePercent");
  const isUp = s ? sChange >= 0 : true;

  // aiAnalysis - 直接从 stock?.aiAnalysis 取，不经过 sanitize
  const ai = (s as any)?.aiAnalysis;
  const zhuangjia = g(ai, "zhuangjia", "主力资金面获取中，规则引擎正在强制计算...");
  const duokong = g(ai, "duokong", "主力资金面获取中，规则引擎正在强制计算...");
  const conclusion = g(ai, "conclusion", "暂无分析");
  const support = gn(ai, "support");
  const resistance = gn(ai, "resistance");

  // plan
  const plan = (ai as any)?.plan || {};
  const planDir = gd(plan, "direction");
  const planEntry = gn(plan, "entry");
  const planSl = gn(plan, "stop_loss");
  const planTarget = gn(plan, "target");
  const planSupp = gn(plan, "supplement");
  const planDetail = g(plan, "detail", "暂无交易计划");

  // causalAnalysis — 因果链
  const causalArr = (ai as any)?.causalAnalysis;
  const hasCausal = Array.isArray(causalArr) && causalArr.length > 0 && causalArr[0]?.cause;

  // moneyFlow + odds
  const mf = (s as any)?.moneyFlow;
  const odds = (s as any)?.odds;

  // chart data — 按周期选择
  // 日K分页：按5个月分组

  const getChartData = (): OHLCVItem[] => {
    if (chartPeriod === "week" && s?.weekly && s.weekly.length > 0) return s.weekly;
    if (chartPeriod === "month" && s?.monthly && s.monthly.length > 0) return s.monthly;
    if (chartPeriod === "fiveDay" && s?.data && s.data.length > 0) return s.data.slice(-5);
    if (s?.data && s.data.length > 0) return s.data;
    if (chartPeriod !== "day" && s?.data && s.data.length > 0) return s.data;
    return defaultMockData;
  };
  const chartData = getChartData();
  const isMockChartData = !s || chartData === defaultMockData;
  const chartMinWidth = chartPeriod === "fiveDay" ? 820 : chartPeriod === "month" ? 1280 : 1500;
  const chartMinHeight = chartPeriod === "fiveDay" ? 720 : 880;
  const usableIndexQuotes = indexQuotes?.filter(
    (q) => typeof q.price === "number" && typeof q.change_pct === "number",
  ) || [];


  // 日期范围
  const dateRangeLabel = (() => {
    const d = chartData;
    if (!d || d.length === 0) return "";
    const first = d[0]?.date || "";
    const last = d[d.length - 1]?.date || "";
    const periodLabel = chartPeriod === "week" ? "周K" : chartPeriod === "month" ? "月K" : chartPeriod === "fiveDay" ? "五日" : "日K";
    return `${first} ~ ${last} · ${periodLabel}`;
  })();

  // ── 搜索 ──
  const search = async () => {
    const trim = code.trim();
    if (!trim) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`/api/stock/${trim}`, { signal: ctrl.signal, timeout: 40000 });
      setStock(res.data as StockResponse);
    } catch (e: any) {
      if (axios.isCancel(e)) return;
      setError(e?.response?.status === 404 ? `未找到股票代码 "${trim}"` : `请求失败: ${e?.message || e}`);
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  };
  const handleKeyDown = (e: React.KeyboardEvent) => { if (e.key === "Enter") search(); };

  // ── 从策略选股跳转 ──
  const handleSelectStock = (code: string) => {
    setCode(code);
    setActiveMode("chart");
    // 触发搜索
    setTimeout(() => {
      const trim = code.trim();
      if (!trim) return;
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setLoading(true);
      setError(null);
      axios.get(`/api/stock/${trim}`, { signal: ctrl.signal, timeout: 40000 })
        .then((res) => setStock(res.data as StockResponse))
        .catch((e) => { if (!axios.isCancel(e)) setError(`请求失败: ${e?.message || e}`); })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    }, 50);
  };

  // 方向标签颜色
  const dirBg =
    planDir === "看多" ? "#7f1d1d" :
    planDir === "看空" ? "#14532d" : "#713f12";
  const dirColor =
    planDir === "看多" ? "#fca5a5" :
    planDir === "看空" ? "#86efac" : "#fde047";

  // Tab 样式
  const tabActive = { color: "#e5e7eb", borderBottom: "2px solid #3b82f6", fontWeight: 600 } as const;
  const tabInactive = { color: "#6b7280", borderBottom: "2px solid transparent", fontWeight: 400, cursor: "pointer" } as const;
  const switchMode = (mode: AppMode) => {
    setActiveMode(mode);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("view", mode);
      window.history.replaceState(null, "", url.toString());
    }
  };

  return (
    <MarketContext.Provider value={marketFull}>
    <div className="flex h-screen bg-bg-base text-text-primary antialiased select-none">

      {/* ── 左侧栏 ── */}
      <aside className="w-80 min-w-80 border-r border-border-default bg-bg-base p-5 overflow-y-auto overflow-x-hidden flex flex-col">

        {/* 品牌标识 */}
        <div className="flex items-center gap-2 mb-3 pb-2 border-b border-border-default/50">
          <div className="w-5 h-5 rounded flex items-center justify-center text-white font-bold text-xs" style={{background:'linear-gradient(135deg,#6366f1,#22d3ee)'}}>B</div>
          <span className="text-sm font-semibold text-text-primary tracking-wide">小彭博社</span>
          <span className="text-[0.55rem] text-text-muted ml-auto">α</span>
        </div>
        {/* 市场状态条 */}
        <MarketHeader />

        {/* 市场气候深度解析 */}
        <MarketContextPanel />

        {/* 板块生命周期 */}
        <SectorLifecyclePanel />

        {/* 搜索框 */}
        <div className="flex items-center gap-2 border border-border-default rounded-lg bg-bg-card px-3 py-2.5 focus-within:border-accent-blue/50 transition-colors">
          <input
            className="flex-1 bg-transparent border-none outline-none text-text-primary text-base font-mono placeholder:text-text-muted"
            placeholder="输入股票代码"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <button
            className="rounded-md bg-accent-blue px-3 py-1.5 border-none text-white cursor-pointer text-sm transition-opacity hover:bg-blue-600 disabled:opacity-50 disabled:cursor-wait"
            onClick={search}
            disabled={loading}
          >
            {loading ? "..." : <Search className="w-5 h-5" />}
          </button>
        </div>

        {/* 错误 */}
        {error && (
          <div className="mt-3 p-3 border border-red-800/60 rounded-lg bg-red-950/20 text-sm text-red-300">
            {error}
          </div>
        )}

        {/* 股票名称/价格 */}
        <div className="mt-5">
          <div className={ `text-xs ${s ? 'text-text-secondary' : 'text-text-muted'}` }>
            {sName} · {sCode}
            <span className={ `ml-1.5 text-[0.625rem] py-0.5 px-1.5 rounded ${s ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40' : 'bg-bg-elevated text-text-muted border border-border-default'}` }>
              {s ? "行情数据" : "模拟展示"}
            </span>
          </div>
          <div className="mt-1 flex items-baseline gap-3">
            <span className={ `text-[1.875rem] font-bold tabular-nums ${isUp ? 'text-accent-red' : 'text-accent-green'}` }>
              ¥{sPrice.toFixed(2)}
            </span>
            <span className={ `text-sm tabular-nums font-medium ${isUp ? 'text-accent-red' : 'text-accent-green'}` }>
              {isUp ? "+" : ""}{sChange.toFixed(2)}
            </span>
            <span className={ `text-sm tabular-nums font-semibold ${isUp ? 'text-accent-red' : 'text-accent-green'}` }>
              {isUp ? "+" : ""}{sChangePct.toFixed(2)}%
            </span>
          </div>
          <div className={ `mt-1 flex items-center gap-2 text-xs ${isUp ? 'text-accent-red' : 'text-accent-green'}` }>
            {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
            {s ? "行情数据 · akshare" : "模拟数据 · 不参与评分"}
          </div>
        </div>

        {/* 均线状态 + MACD */}
        {s?.snapshot && (
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
            <div className="rounded bg-bg-card px-2 py-1.5 text-text-secondary">
              均线: <span className="text-text-primary">
                {s.snapshot.alignment && s.snapshot.alignment !== "数据不足"
                  ? s.snapshot.alignment
                  : "正在同步"}
              </span>
            </div>
            <div className="rounded bg-bg-card px-2 py-1.5 text-text-secondary">
              MACD: <span className={ s.snapshot.dif != null && s.snapshot.dif >= 0 ? 'text-accent-red' : 'text-accent-green' }>
                {s.snapshot.dif != null && s.snapshot.macd != null
                  ? `${s.snapshot.macd.toFixed(3)}`
                  : "趋势未形成"}
              </span>
            </div>
          </div>
        )}

        {/* 资金流向 */}
        {mf && (
          <div className="mt-4 rounded-lg border border-border-default bg-bg-card/50 p-3">
            <div className="mb-2 text-xs font-semibold text-text-secondary">当日资金流向</div>
            {[
              ["主力净流入", mf.main_net, mf.main_pct],
              ["超大单", mf.super_large_net, null],
              ["大单", mf.large_net, null],
              ["中单", mf.medium_net, null],
              ["散户", mf.small_net, null],
            ].map(([label, val, pct]: any) => (
              <div key={label} className="flex justify-between text-xs py-1">
                <span className="text-text-muted">{label}</span>
                <span className={ val >= 0 ? 'text-accent-red' : 'text-accent-green' }>
                  {val === 0 ? "—" : `${(val / 1e4).toFixed(0)}万`}
                  {pct != null ? ` (${Number(pct).toFixed(1)}%)` : ""}
                </span>
              </div>
            ))}
          </div>
        )}

        <hr className="my-4 border-0 border-t border-border-default" />

        {/* 分析内容 */}
        <div className="flex-1 flex flex-col gap-5">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-text-secondary">
              <span className="inline-block w-2 h-2 rounded-full bg-accent-blue glow-pulse"></span>
              AI 市场认知系统分析中...
            </div>
          ) : (
            <>
              {/* ── AI 赔率评估卡 ── */}
              {odds && <OddsCard odds={odds} />}

              {/* 模块1: 庄家意图拆解 */}
              <div className="rounded-lg border border-accent-orange/40 bg-orange-950/15 p-4">
                <div className="flex items-center gap-2 mb-2 text-sm font-bold text-accent-orange/90">
                  <span className="inline-flex w-5 h-5 items-center justify-center rounded bg-orange-500/50 text-xs">🎯</span>
                  庄家意图拆解
                </div>
                <p className="text-sm leading-relaxed text-text-primary/90 m-0 whitespace-pre-wrap">{zhuangjia}</p>
              </div>

              {/* 模块2: 主力多空博弈 */}
              <div className="rounded-lg border border-accent-purple/40 bg-purple-950/15 p-4">
                <div className="flex items-center gap-2 mb-2 text-sm font-bold text-accent-purple/80">
                  <span className="inline-flex w-5 h-5 items-center justify-center rounded bg-purple-500/50 text-xs">⚔️</span>
                  主力多空博弈
                </div>
                <p className="text-sm leading-relaxed text-text-primary/90 m-0 whitespace-pre-wrap">{duokong}</p>
              </div>

              {/* 模块3: 明日交易计划 */}
              <div className="rounded-lg border border-accent-cyan/40 bg-cyan-950/15 p-4">
                <div className="flex items-center gap-2 mb-2 text-sm font-bold text-accent-cyan">
                  <span className="inline-flex w-5 h-5 items-center justify-center rounded bg-cyan-500/50 text-xs">📋</span>
                  明日分步交易计划
                  <span className={ `ml-auto text-[0.625rem] py-0.5 px-1.5 rounded font-semibold ${
                    planDir === "看多" ? 'bg-red-900/60 text-red-300' :
                    planDir === "看空" ? 'bg-green-900/60 text-green-300' :
                    'bg-yellow-900/60 text-yellow-300'
                  }` }>
                    {planDir}
                  </span>
                </div>
                <p className="text-sm leading-relaxed text-text-primary/90 m-0 whitespace-pre-wrap">{planDetail}</p>
                {/* 价格四格表 */}
                <div className="mt-3 grid grid-cols-4 gap-2">
                  <PriceBox label="入场" value={planEntry} color="#22d3ee" />
                  <PriceBox label="止损" value={planSl} color="#f87171" />
                  <PriceBox label="目标" value={planTarget} color="#34d399" />
                  <PriceBox label="补仓" value={planSupp} color="#facc15" />
                </div>
              </div>
            </>
          )}

          {/* ── 因果推理链 ── */}
          {hasCausal && (
            <div className="rounded-lg border border-border-default bg-bg-card/50 p-4">
              <div className="mb-2.5 flex items-center gap-1.5">
                <span className="text-xs font-semibold text-accent-cyan">🔗 因果推理</span>
              </div>
              {causalArr.map((chain: any, idx: number) => (
                <div key={idx} className={ idx < causalArr.length - 1 ? 'mb-3 pb-3 border-b border-white/5' : '' }>
                  {/* cause */}
                  <div className="flex items-start gap-1.5 mb-1.5">
                    <span className="text-[0.65rem] font-semibold text-accent-red min-w-[1.4rem]">因 </span>
                    <span className="text-[0.775rem] leading-relaxed text-text-primary/90">{chain.cause}</span>
                  </div>
                  {/* effect */}
                  <div className="flex items-start gap-1.5 mb-1.5">
                    <span className="text-[0.65rem] font-semibold text-accent-amber min-w-[1.4rem]">果 </span>
                    <span className="text-[0.775rem] leading-relaxed text-text-primary/90">{chain.effect}</span>
                  </div>
                  {/* implication */}
                  <div className="flex items-start gap-1.5">
                    <span className="text-[0.65rem] font-semibold text-accent-green min-w-[1.4rem]">策 </span>
                    <span className="text-[0.775rem] leading-relaxed text-text-secondary italic">{chain.implication}</span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* 技术面补充 */}
          <div className="rounded-lg border border-border-default bg-bg-card/50 p-4">
            <div className="mb-2 text-xs font-semibold text-text-secondary">技术面补充</div>
            <p className="text-sm leading-relaxed text-text-primary/90 m-0 whitespace-pre-wrap">{conclusion}</p>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <MiniBox label="压力位" value={resistance} color="#f87171" icon="🎯" />
              <MiniBox label="支撑位" value={support} color="#34d399" icon="🛡️" />
            </div>
          </div>

          {/* 免责 */}
          <div className="rounded-lg border border-border-default bg-bg-base/30 p-3">
            <p className="text-xs leading-relaxed text-text-muted m-0">
              以上分析由{s ? " DeepSeek AI" : "规则引擎"}生成，基于市场认知系统(Cognitive Market Intelligence)，仅供参考，不构成任何投资建议。
              股市有风险，投资需谨慎。
            </p>
          </div>
        </div>
      </aside>

      {/* ── 右侧主区域 ── */}
      <main className="flex-1 flex flex-col overflow-hidden">

        {/* 顶部 Header + Tab 栏 */}
        <header className="glass border-b border-border-default flex flex-col">
          {/* 第一行：标题 + Tab */}
          <div className="flex items-center justify-between px-6 pt-3 pb-1.5">
            <div className="flex items-center gap-6">
              <h1 className="text-lg font-bold tracking-tight m-0 text-gradient">Claw Capital</h1>
              <div className="flex gap-0">
                {[
                  { key: "dashboard" as const, icon: <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>, label: "决策面板" },
                  { key: "terminal" as const, icon: <Terminal className="w-4 h-4" />, label: "Alpha Terminal" },
                  { key: "chart" as const, icon: <ChartArea className="w-4 h-4" />, label: "行情分析" },
                  { key: "screener" as const, icon: <ListFilter className="w-4 h-4" />, label: "策略选股" },
                ].map(t => (
                  <button key={t.key} onClick={() => switchMode(t.key)}
                    className={`flex items-center gap-2.5 px-4 py-2 border-none bg-transparent text-sm transition-all cursor-pointer ${
                      activeMode === t.key
                        ? 'text-text-primary border-b-2 border-accent-blue font-semibold'
                        : 'text-text-muted border-b-2 border-transparent hover:text-text-secondary'
                    }`}>
                    {t.icon}
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
          {/* 第二行：指数行情 + 涨跌家数 */}
          <div className="flex items-center justify-between px-6 pb-2.5 gap-4">
            {/* 左：涨跌家数 */}
            <div className="flex items-center gap-3">
              {marketSummary && marketSummary.total > 0 ? (
                <>
                  <span className="text-xs text-text-muted font-semibold">📈 涨跌家数</span>
                  <span className="text-xs font-semibold text-accent-red tabular-nums">↑{marketSummary.rise}</span>
                  <span className="text-xs text-text-muted">/</span>
                  <span className="text-xs font-semibold text-accent-green tabular-nums">↓{marketSummary.fall}</span>
                  <span className="text-xs text-text-muted ml-0.5">共{marketSummary.total}家</span>
                </>
              ) : (
                <span className="text-xs text-text-muted">📊 涨跌家数加载中...</span>
              )}
            </div>
            {/* 右：指数行情 */}
            <div className="flex gap-3 items-center overflow-x-auto max-w-[54vw]">
              {usableIndexQuotes.map((q: any) => (
                <div key={q.name} className="flex items-center gap-2 whitespace-nowrap px-2 py-1 rounded bg-bg-elevated/80">
                  <span className="text-text-secondary font-semibold text-xs">{q.name}</span>
                  <span className="text-text-primary font-mono text-sm font-semibold tabular-nums">{q.price.toFixed(2)}</span>
                  <span className={`font-bold text-xs ${q.change_pct >= 0 ? 'text-accent-red' : 'text-accent-green'}`}>
                    {q.change_pct >= 0 ? "+" : ""}{q.change_pct.toFixed(2)}%
                  </span>
                </div>
              ))}
              {indexQuotes && usableIndexQuotes.length === 0 && (
                <span className="text-xs text-text-muted whitespace-nowrap rounded bg-bg-elevated/70 px-2 py-1">
                  指数行情同步中
                </span>
              )}
              {indexQuotes === null && (
                <span className="text-xs text-text-muted">📊 加载指数...</span>
              )}
            </div>
          </div>
        </header>

        {/* 内容区 */}
        {(() => {
          switch (activeMode) {
            case "dashboard":
              return <DashboardPanel />;
            case "terminal":
              return (
                <div className="flex-1 overflow-hidden">
                  <AlphaTerminal />
                </div>
              );
            case "chart":
              return (
                <div className="flex-1 overflow-auto p-4 pt-3">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div className="inline-flex gap-1 bg-bg-elevated rounded-md p-0.5 shrink-0">
                      {(["day", "week", "month", "fiveDay"] as const).map((p) => {
                        const labels: Record<string, string> = { day: "日K", week: "周K", month: "月K", fiveDay: "五日" };
                        return (
                          <button key={p} onClick={() => setChartPeriod(p)}
                            className={`px-3 py-1.5 rounded text-[0.75rem] border-none cursor-pointer transition-all ${
                              chartPeriod === p
                                ? 'bg-bg-hover text-text-primary font-semibold'
                                : 'bg-transparent text-text-muted hover:text-text-secondary'
                            }`}>
                            {labels[p]}
                          </button>
                        );
                      })}
                    </div>
                    <span className="text-[0.68rem] text-text-muted whitespace-nowrap">
                      {chartPeriod === "fiveDay" ? "五日只展示短线K线与成交量" : "横向滚动查看完整技术图层"}
                    </span>
                  </div>
                  <div className="overflow-x-auto overflow-y-visible rounded-lg border border-border-default/60">
                    <div
                      style={{ minWidth: chartMinWidth, height: chartMinHeight }}
                      className="pb-3"
                    >
                      {!s ? (
                        <div className="h-full min-h-[420px] flex items-center justify-center bg-bg-card text-text-muted text-sm">
                          正在加载真实行情，未取得数据前不展示模拟K线
                        </div>
                      ) : (
                        <StockChart key={chartPeriod + String(chartData.length)} data={chartData} period={chartPeriod} title={dateRangeLabel} dataSource={isMockChartData ? "mock" : "live"} />
                      )}
                    </div>
                  </div>
                  {s && <TechnicalIndicatorReadout data={chartData} period={chartPeriod} />}
                </div>
              );
            case "screener":
              return (
                <div className="flex-1 overflow-hidden">
                  <StrategyScreener onSelectStock={handleSelectStock} />
                </div>
              );
          }
        })()}
      </main>
    </div>
    </MarketContext.Provider>
  );
}

// ── Dashboard: 今日决策面板 ──
function DashboardPanel() {
  const [summary, setSummary] = useState<string | null>(null);
  const [screenerData, setScreenerData] = useState<any[]>([]);
  const [portfolio, setPortfolio] = useState<any[]>([]);
  const [portfolioInput, setPortfolioInput] = useState("");
  const [searchHistory, setSearchHistory] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("portfolio_search_history") || "[]"); }
    catch { return []; }
  });
  const [hotNews, setHotNews] = useState<any[] | null>(null);
  const [newsflash, setNewsflash] = useState<any[] | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [hotNewsLoading, setHotNewsLoading] = useState(true);
  const [newsflashLoading, setNewsflashLoading] = useState(true);

  const fetchSummary = useRef(async () => {
    setSummaryLoading(true);
    try {
      const [sRes, aRes] = await Promise.all([
        axios.get("/api/alpha/summary", { timeout: 5000 }),
        axios.get("/api/alpha/candidates?max=15", { timeout: 8000 }),
      ]);
      setSummary(sRes.data.text);
      const candidates = aRes.data.candidates || aRes.data.screener || [];
      setScreenerData(candidates.map((item: any, index: number) => ({
        ...item,
        rank: item.rank ?? index + 1,
        code: item.code ?? item.symbol ?? "",
        odds_ratio: Number(item.odds_ratio ?? item.risk_reward ?? 0),
        adjusted_odds: Number(item.adjusted_odds ?? item.risk_reward ?? 0),
        price: typeof item.price === "number" ? item.price : null,
        score: Number(item.score ?? 0),
      })));
    } catch {
      setSummary((prev) => prev ?? "Alpha 简报正在后台生成，热点与行情可先查看。");
      setScreenerData([]);
    } finally {
      setSummaryLoading(false);
    }
  });
  const fetchHotNews = useRef(async () => {
    setHotNewsLoading(true);
    try {
      const res = await axios.get("/api/hot/news", { timeout: 5000 });
      setHotNews(res.data || []);
    } catch { setHotNews([]); }
    finally { setHotNewsLoading(false); }
  });
  const fetchNewsflash = useRef(async () => {
    setNewsflashLoading(true);
    try {
      const res = await axios.get("/api/hot/newsflash", { timeout: 5000 });
      setNewsflash(res.data || []);
    } catch { setNewsflash([]); }
    finally { setNewsflashLoading(false); }
  });
  useEffect(() => {
    fetchSummary.current();
    fetchHotNews.current();
    fetchNewsflash.current();
    // 盘中(9:30-15:00)每 2min 刷新一次, 盘后每 10min 刷新一次
    const h = new Date().getHours();
    const isTradingHours = h >= 9 && h < 15;
    const interval = setInterval(() => {
      fetchHotNews.current();
      fetchNewsflash.current();
    }, isTradingHours ? 120000 : 600000);
    return () => clearInterval(interval);
  }, []);

  const runPortfolio = async (codes?: string[]) => {
    const targetCodes = codes ?? portfolioInput.split(/[,，\s]+/).filter(Boolean);
    if (targetCodes.length === 0) return;
    // 存历史
    const newCodes = targetCodes.filter(c => !searchHistory.includes(c));
    const updated = [...newCodes, ...searchHistory].slice(0, 20);
    setSearchHistory(updated);
    try { localStorage.setItem("portfolio_search_history", JSON.stringify(updated)); } catch {}
    try {
      const res = await axios.post("/api/alpha/portfolio", { positions: targetCodes.map(c => ({ code: c, name: c })) }, { timeout: 15000 });
      setPortfolio(res.data.positions || []);
    } catch {}
  };
  const removeHistoryItem = (code: string) => {
    const updated = searchHistory.filter(c => c !== code);
    setSearchHistory(updated);
    try { localStorage.setItem("portfolio_search_history", JSON.stringify(updated)); } catch {}
  };
  const clearHistory = () => {
    setSearchHistory([]);
    try { localStorage.setItem("portfolio_search_history", "[]"); } catch {}
  };

  return (
    <div className="flex-1 overflow-auto p-6 space-y-4">
      {/* 执行简报 */}
      {summaryLoading && (
        <div className="rounded-lg border border-border-default bg-bg-elevated p-4 text-xs text-text-secondary">
          Alpha 简报正在后台生成，其他模块已先行加载...
        </div>
      )}
      {summary && (
        <div className="rounded-lg border border-border-default bg-bg-elevated p-4 text-xs leading-relaxed whitespace-pre-line font-mono">
          {summary.split("\n").map((line, i) => {
            const isHeader = line.startsWith("📊") || line.startsWith("✅") || line.startsWith("🔴") || line.startsWith("⚠️") || line.startsWith("💡") || line.startsWith("🎯") || line.startsWith("👀");
            return (
              <div key={i} style={{ marginBottom: "0.125rem", color: isHeader ? "#e2e8f0" : "#9ca3af" }}>
                {line || "\u00a0"}
              </div>
            );
          })}
        </div>
      )}

      {/* 精选池 */}
      {screenerData.length > 0 && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4">
          <div className="text-base font-semibold text-text-primary mb-4">🎯 今日Alpha精选池</div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="text-text-muted text-left">
                  <th className="py-1.5 px-2 border-b border-border-default">#</th>
                  <th className="py-1.5 px-2 border-b border-border-default">代码</th>
                  <th className="py-1.5 px-2 border-b border-border-default">板块</th>
                  <th className="py-1.5 px-2 border-b border-border-default">赔率</th>
                  <th className="py-1.5 px-2 border-b border-border-default">调整后</th>
                  <th className="py-1.5 px-2 border-b border-border-default">评分</th>
                  <th className="py-1.5 px-2 border-b border-border-default">价格</th>
                </tr>
              </thead>
              <tbody>
                {screenerData.map((item: any, si: number) => (
                  <tr key={`scr-${item.code}-${si}`} className="border-b border-border-default/50">
                    <td className="py-2 px-2 text-text-muted">{item.rank}</td>
                    <td className="py-1.5 px-2 text-text-primary font-semibold">{item.code}</td>
                    <td className="py-1.5 px-2 text-text-secondary">{item.sector}</td>
                    <td className={`py-1.5 px-2 ${item.odds_ratio >= 2 ? 'text-accent-green' : 'text-accent-amber'}`}>{item.odds_ratio.toFixed(2)}</td>
                    <td className={`py-1.5 px-2 ${item.adjusted_odds >= 2 ? 'text-accent-green' : 'text-accent-amber'}`}>{item.adjusted_odds.toFixed(2)}</td>
                    <td className={`py-1.5 px-2 ${item.score >= 50 ? 'text-accent-green' : item.score >= 20 ? 'text-accent-amber' : 'text-text-muted'}`}>{item.score}</td>
                    <td className="py-1.5 px-2 text-text-primary/80">¥{item.price?.toFixed(2) ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 持仓诊断 */}
      <div className="rounded-lg border border-border-default bg-bg-card p-4">
        <div className="text-base font-semibold text-text-primary mb-3">🔍 持仓诊断</div>
        <div className="flex gap-2 mb-4">
          <input
            value={portfolioInput}
            onChange={e => setPortfolioInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && runPortfolio()}
            placeholder="输入持仓代码，以逗号分隔: 300750, 600941, 002594"
            className="flex-1 px-4 py-3 rounded-lg border border-border-default bg-bg-elevated text-text-primary text-base outline-none focus:border-accent-blue/60 transition-colors placeholder:text-text-muted"
          />
          <button onClick={() => runPortfolio()} className="px-5 py-3 rounded-lg border-none bg-accent-blue text-white font-semibold text-base cursor-pointer hover:bg-blue-600 transition-colors">
            诊断
          </button>
        </div>
        {/* 搜索历史 */}
        {searchHistory.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-3 mt-2 items-center">
            <span className="text-[0.6875rem] text-text-muted">最近搜索:</span>
            {searchHistory.slice(0, 12).map((code, si) => (
              <span key={`${code}-${si}`}
                className="inline-flex items-center gap-1 py-0.5 px-2 rounded-full bg-bg-elevated border border-border-default text-xs text-text-secondary cursor-pointer select-none hover:border-text-muted transition-colors"
                onClick={() => { setPortfolioInput(code); runPortfolio([code]); }}>
                {code}
                <span onClick={e => { e.stopPropagation(); removeHistoryItem(code); }}
                  className="text-[0.625rem] text-text-muted cursor-pointer leading-none" title="删除">✕</span>
              </span>
            ))}
            <span onClick={clearHistory}
              className="text-[0.6875rem] text-text-muted cursor-pointer underline underline-offset-2 decoration-border-default ml-1 hover:text-text-secondary">清空</span>
          </div>
        )}
        {portfolio.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="text-text-muted text-left">
                  <th className="py-1.5 px-2 border-b border-border-default">代码</th>
                  <th className="py-1.5 px-2 border-b border-border-default">板块</th>
                  <th className="py-1.5 px-2 border-b border-border-default">周期</th>
                  <th className="py-1.5 px-2 border-b border-border-default">健康</th>
                  <th className="py-1.5 px-2 border-b border-border-default">诊断</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.map((p: any, i: number) => {
                  const healthColors: Record<string, string> = {
                    healthy: "text-accent-green", inefficient: "text-accent-amber", at_risk: "text-accent-red",
                  };
                  const healthLabels: Record<string, string> = {
                    healthy: "✅ 健康", inefficient: "⚠️ 关注", at_risk: "🔴 风险",
                  };
                  const hc = healthColors[p.health] || "text-text-muted";
                  const hl = healthLabels[p.health] || "未知";
                  return (
                    <tr key={i} className="border-b border-border-default/50">
                      <td className="py-1.5 px-2 text-text-primary font-semibold">{p.code}</td>
                      <td className="py-1.5 px-2 text-text-secondary">{p.sector}</td>
                      <td className="py-1.5 px-2 text-text-secondary">{p.sector_phase}</td>
                      <td className={`py-1.5 px-2 font-semibold ${hc}`}>{hl}</td>
                      <td className="py-1.5 px-2 text-text-primary/80 max-w-72">{p.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {hotNewsLoading && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4 text-sm text-text-secondary">
          正在加载市场热点...
        </div>
      )}

      {/* 热点事件 */}
      {!hotNewsLoading && hotNews !== null && hotNews.length === 0 && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4 text-sm text-text-muted">
          市场热点暂不可用，后台会继续刷新。
        </div>
      )}
      {!hotNewsLoading && hotNews !== null && hotNews.length > 0 && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-base font-semibold text-text-primary">🔥 市场热点</div>
            <span className="text-[0.6rem] text-text-muted">百度热搜 · 点击名称查看行情</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="text-text-muted text-left">
                  <th className="py-2 px-2 border-b border-border-default w-8">#</th>
                  <th className="py-1.5 px-2 border-b border-border-default">名称</th>
                  <th className="py-1.5 px-2 border-b border-border-default text-right">涨跌幅</th>
                  <th className="py-1.5 px-2 border-b border-border-default text-right">热度</th>
                </tr>
              </thead>
              <tbody>
                {hotNews.slice(0, 12).map((item: any, i: number) => (
                  <tr key={i} className="border-b border-border-default/50 hover:bg-bg-card/60">
                    <td className="py-2 px-2 text-text-muted">{i + 1}</td>
                    <td className="py-1.5 px-2">
                      <a
                        href={item.code ? `https://quote.eastmoney.com/${item.code.startsWith("6") || item.code.startsWith("688") || item.code.startsWith("689") ? "sh" : "sz"}${item.code}.html` : `https://www.baidu.com/s?wd=${encodeURIComponent(item.name + " 股票")}`}
title={`${item.name} ${typeof item.change_pct === "number" ? (item.change_pct >= 0 ? "+" : "") + item.change_pct.toFixed(2) + "%" : ""}`}
                                                className="text-accent-blue hover:text-accent-cyan hover:underline font-semibold transition-colors cursor-pointer"
                      >{item.name}</a>
                    </td>
                    <td className={`py-1.5 px-2 text-right font-semibold tabular-nums ${item.change_pct >= 0 ? 'text-accent-red' : 'text-accent-green'}`}>
                      {typeof item.change_pct === 'number'
                        ? (item.change_pct >= 0 ? '+' : '') + item.change_pct.toFixed(2) + '%'
                        : item.change_pct}
                    </td>
                    <td className="py-1.5 px-2 text-right text-text-muted tabular-nums">{item.heat?.toLocaleString?.() ?? item.heat}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {newsflashLoading && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4 text-sm text-text-secondary">
          正在加载重要新闻...
        </div>
      )}

      {/* 重要新闻 */}
      {!newsflashLoading && newsflash !== null && newsflash.length === 0 && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4 text-sm text-text-muted">
          重要新闻暂不可用，后台会继续刷新。
        </div>
      )}
      {!newsflashLoading && newsflash !== null && newsflash.length > 0 && (
        <div className="rounded-lg border border-border-default bg-bg-card p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-base font-semibold text-text-primary">📰 重要新闻</div>
            <span className="text-[0.6rem] text-text-muted">财信快讯 · 当日</span>
          </div>
          <div className="flex flex-col max-h-[480px] overflow-y-auto overflow-x-hidden pr-1">
            {newsflash.slice(0, 30).map((item: any, i: number) => {
              const _tc = ((t: string) => {
                const m: Record<string, {bg:string,text:string}> = {
                  "今日热点": { bg: "rgba(251,146,60,0.15)", text: "#fb923c" },
                  "市场动态": { bg: "rgba(59,130,246,0.12)", text: "#60a5fa" },
                  "市场洞察": { bg: "rgba(59,130,246,0.12)", text: "#60a5fa" },
                  "权益周观察": { bg: "rgba(168,85,247,0.12)", text: "#c084fc" },
                  "公司速递": { bg: "rgba(34,197,94,0.12)", text: "#4ade80" },
                  "华尔街原声": { bg: "rgba(250,204,21,0.12)", text: "#facc15" },
                  "商圈": { bg: "rgba(107,114,128,0.12)", text: "#9ca3af" },
                  "CCI快报": { bg: "rgba(6,182,212,0.12)", text: "#22d3ee" },
                  "数据图解": { bg: "rgba(34,197,94,0.12)", text: "#4ade80" },
                  "周刊提前读": { bg: "rgba(168,85,247,0.12)", text: "#c084fc" },
                };
                return m[t] || { bg: "rgba(107,114,128,0.15)", text: "#9ca3af" };
              })(item.tag);
              const bg = _tc.bg;
              const text = _tc.text;
              return (
                <a
                  key={i}
                  href={item.url || `https://www.baidu.com/s?wd=${encodeURIComponent(item.text)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex flex-col gap-0.5 border-b border-border-default/30 py-2.5 last:border-b-0 hover:bg-white/5 -mx-4 px-4 transition-colors cursor-pointer text-accent-blue hover:text-accent-cyan"
                >
                  <div className="flex items-center gap-1.5">
                    <span className="shrink-0 px-1 py-0.5 rounded text-[0.55rem] font-semibold tracking-wide"
                      style={{ backgroundColor: bg, color: text }}>{item.tag || "资讯"}</span>
                    {item.date && (
                      <span className="text-[0.55rem] text-text-muted/50 font-mono">{item.date.slice(5)}</span>
                    )}
                  </div>
                  <div className="text-[0.7rem] text-text-primary/85 leading-relaxed ml-0.5 line-clamp-2">
                    {item.text}
                  </div>
                </a>
              );
            })}

          </div>
        </div>
      )}
    </div>
  );
}

// ── 小组件 ──
function PriceBox({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="rounded bg-bg-card/80 p-1.5 text-center">
      <div className="text-[0.625rem] text-text-muted">{label}</div>
      <div style={{ fontSize: "0.875rem", fontWeight: 700, color }}>¥{value.toFixed(2)}</div>
    </div>
  );
}
function MiniBox({ label, value, color, icon }: { label: string; value: number; color: string; icon: string }) {
  return (
    <div className="rounded-lg border border-red-900/40 bg-red-950/20 p-2.5">
      <div className="flex items-center gap-1.5 text-xs" style={{ color }}>
        <span>{icon}</span>
        {label}
      </div>
      <div className="mt-0.5 text-base font-bold tabular-nums" style={{ color }}>
        ¥{value.toFixed(2)}
      </div>
    </div>
  );
}

function TechnicalIndicatorReadout({ data, period }: { data: OHLCVItem[]; period: "day" | "week" | "month" | "fiveDay" }) {
  const closes = data.map((d) => Number(d.close)).filter(Number.isFinite);
  const volumes = data.map((d) => Number(d.volume)).filter(Number.isFinite);
  const last = closes[closes.length - 1] ?? 0;
  const prev = closes[closes.length - 2] ?? last;
  const ma5 = lastAverage(closes, 5);
  const ma10 = lastAverage(closes, 10);
  const ma20 = lastAverage(closes, 20);
  const ma60 = lastAverage(closes, 60);
  const vol5 = lastAverage(volumes, 5);
  const vol20 = lastAverage(volumes, 20);
  const macd = computeMacdPoint(closes);
  const periodLabel = period === "week" ? "周线" : period === "month" ? "月线" : period === "fiveDay" ? "五日" : "日线";

  const maText = (() => {
    if (period === "fiveDay") return "五日视图样本太短，只看最近价格/成交量变化，不做均线趋势判断。";
    if (ma5 != null && ma10 != null && ma20 != null && ma5 > ma10 && ma10 > ma20 && last > ma5) {
      return "短中期均线呈多头排列，价格在 MA5 上方，趋势结构偏强；若跌回 MA10/MA20 下方，需要降低趋势判断。";
    }
    if (ma5 != null && ma10 != null && ma20 != null && ma5 < ma10 && ma10 < ma20 && last < ma5) {
      return "短中期均线呈空头排列，价格在均线下方，反弹容易遇到抛压。";
    }
    return "均线相互缠绕或价格贴近均线，说明趋势不够顺，更多是震荡结构。";
  })();

  const volumeRatio = vol5 && vol20 ? vol5 / vol20 : null;
  const volumeText = volumeRatio == null
    ? "成交量样本不足，暂不评价量能。"
    : volumeRatio > 1.35
      ? `近5期均量是20期均量的 ${volumeRatio.toFixed(2)} 倍，量能明显放大，需要结合价格位置判断是突破还是分歧放量。`
      : volumeRatio < 0.75
        ? `近5期均量只有20期均量的 ${volumeRatio.toFixed(2)} 倍，资金参与度偏低，趋势延续性要打折。`
        : `近5期均量/20期均量为 ${volumeRatio.toFixed(2)}，量能处于相对正常区间。`;

  const macdText = (() => {
    if (period === "fiveDay" || macd.dif == null || macd.dea == null || macd.macd == null) {
      return "MACD 需要更长样本，五日/短样本不展示结论。";
    }
    const expanding = macd.prevMacd != null ? Math.abs(macd.macd) > Math.abs(macd.prevMacd) : false;
    if (macd.dif > macd.dea && macd.dif > 0) return `DIF 在 DEA 上方且位于零轴上，动能偏多；${expanding ? "红柱扩张，短线动能仍在增强。" : "柱体未明显扩张，追高要谨慎。"}`;
    if (macd.dif > macd.dea && macd.dif <= 0) return "DIF 在 DEA 上方但仍在零轴下，属于弱反弹结构，不能等同于趋势反转。";
    if (macd.dif < macd.dea && macd.dif > 0) return "DIF 在 DEA 下方但仍在零轴上，偏高位降速，适合观察是否跌破关键均线。";
    return "DIF 在 DEA 下方且处于零轴下，空头动能占优。";
  })();

  const changeText = prev > 0 ? `${last >= prev ? "+" : ""}${(((last - prev) / prev) * 100).toFixed(2)}%` : "--";
  const ma60Text = ma60 != null ? `MA60 ${ma60.toFixed(2)}` : "MA60 样本不足";

  return (
    <section className="mt-3 rounded-lg border border-border-default bg-bg-card p-4 text-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="font-semibold text-text-primary">技术指标解读</div>
        <div className="text-xs text-text-muted">{periodLabel} · 最新涨跌 {changeText} · {ma60Text}</div>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        <ReadoutBlock title="均线结构" text={maText} />
        <ReadoutBlock title="量能状态" text={volumeText} />
        <ReadoutBlock title="MACD 动能" text={macdText} />
      </div>
      <div className="mt-3 text-xs leading-relaxed text-text-muted">
        以上是基于当前周期 K 线的技术状态解读，不是买卖建议；五日视图只用于观察最近五个交易日，不参与中长期指标判断。
      </div>
    </section>
  );
}

function ReadoutBlock({ title, text }: { title: string; text: string }) {
  return (
    <div className="rounded-md border border-border-default/70 bg-bg-base/40 p-3">
      <div className="mb-1 text-xs font-semibold text-text-secondary">{title}</div>
      <div className="text-xs leading-relaxed text-text-primary/90">{text}</div>
    </div>
  );
}

function OddsCard({ odds }: { odds: { upside_pct: number; downside_pct: number; odds_ratio: number; rating: string } }) {
  const ratio = odds.odds_ratio;
  const full = useContext(MarketContext);
  const factor = full?.environment?.adjustment_factor ?? 1.0;
  const adjustedRatio = ratio * factor;
  const color = adjustedRatio >= 2.0 ? "#22c55e" : adjustedRatio >= 1.0 ? "#eab308" : "#ef4444";
  const bgColor = adjustedRatio >= 2.0 ? "rgba(34,197,94,0.12)" : adjustedRatio >= 1.0 ? "rgba(234,179,8,0.12)" : "rgba(239,68,68,0.12)";
  const ratingLabel = odds.rating || (adjustedRatio >= 2.0 ? "高赔率" : adjustedRatio >= 1.0 ? "合理" : "低赔率");
  return (
    <div className="rounded-lg p-4 mb-3" style={{ border: `1px solid ${color}44`, backgroundColor: bgColor }}>
      <div className="flex justify-between items-center mb-2">
        <div className="text-xs font-semibold text-text-secondary">📊 AI 赔率评估</div>
        <span className="text-[0.65rem] py-0.5 px-2 rounded" style={{ backgroundColor: `${color}22`, color }}>{ratingLabel}</span>
      </div>
      <div className="text-2xl font-bold text-center mb-2.5" style={{ color }}>
        {adjustedRatio.toFixed(2)}
        <span className="text-sm font-normal text-text-muted ml-1">倍</span>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="text-text-secondary">
          上涨预期{" "}
          <span className="font-semibold text-accent-green">+{odds.upside_pct}%</span>
        </div>
        <div className="text-text-secondary">
          下行风险{" "}
          <span className="font-semibold text-accent-red">-{odds.downside_pct}%</span>
        </div>
      </div>
      {factor !== 1.0 && (
        <div className="mt-1.5 text-[0.65rem] text-center border-t border-border-default/50 pt-1.5" style={{ color: factor > 1 ? "#4ade80" : "#f87171" }}>
          {factor > 1
            ? `⚡ 市场情绪积极，赔率上调 ${Math.round((factor - 1) * 100)}%`
            : `⚠️ 当前市场防守模式，赔率已下调 ${Math.round((1 - factor) * 100)}%`
          }
        </div>
      )}
    </div>
  );
}
