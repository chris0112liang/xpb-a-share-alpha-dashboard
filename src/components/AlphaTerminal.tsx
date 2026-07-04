import { useCallback, useEffect, useMemo, useState } from "react";
import axios from "axios";

interface Candidate {
  symbol: string;
  name: string;
  sector: string;
  score: number;
  confidence: number;
  tier: string;
  risk_reward: number;
  triggered_strategy_display?: string;
  sector_phase_cn?: string;
  reasons?: string[];
}

interface MarketEvent {
  event_type: string;
  severity: string;
  confidence?: number;
  sector?: string;
  description: string;
}

interface AiExplanation {
  regime?: string;
  rotation_analysis?: string;
  risk_assessment?: string;
  strategy_rationale?: string;
  warning?: string;
}

interface SectorInfo {
  phase: string;
  confidence: number;
  bias: number;
  strength_score: number;
  price_mom_5?: number;
  vol_trend?: number;
  data_rows?: number;
  is_turning?: boolean;
}

interface TerminalData {
  ts: string;
  regime: string;
  regime_display: string;
  active_strategies: string[];
  primary_strategy: string;
  leading_sectors: string[];
  rotation_speed: number;
  risk_level: number;
  market_bias: number;
  turning_alerts: any[];
  top_candidates: Candidate[];
  market_events: MarketEvent[];
  ai_summary: string;
  ai_explanation: AiExplanation;
  computed_at: string;
  data_source?: string;
  world_state?: {
    sector_heatmap?: {
      phase_distribution?: Record<string, number>;
      total_sectors?: number;
      top_momentum?: Array<Record<string, any>>;
    };
    lifecycles?: Record<string, SectorInfo>;
    breadth_score?: number;
    momentum_score?: number;
    liquidity_state?: string;
    volatility_state?: string;
    dominant_style?: string;
  };
}

interface CandidateReport {
  total_candidates: number;
  candidates: Candidate[];
  primary_strategy: string;
  active_strategies: string[];
  explanation: string;
  scan_time: string;
}

interface MarketFullResponse {
  lifecycles?: {
    sectors?: Record<string, SectorInfo>;
    summary?: Record<string, number>;
    total?: number;
  };
}

const PHASE_LABELS: Record<string, string> = {
  ice_recovery: "冰点修复",
  startup: "启动期",
  main_rise_1: "主升一期",
  acceleration: "加速期",
  high_divergence: "高位分歧",
  decay: "退潮期",
  noise: "横盘震荡",
  unknown: "数据不足",
  detect_failed: "感知中断",
};

const PHASE_COLORS: Record<string, string> = {
  ice_recovery: "#06b6d4",
  startup: "#22c55e",
  main_rise_1: "#eab308",
  acceleration: "#f97316",
  high_divergence: "#fb923c",
  decay: "#ef4444",
  noise: "#6b7280",
  unknown: "#9ca3af",
  detect_failed: "#dc2626",
};

const STRATEGY_LABELS: Record<string, string> = {
  trend_breakout: "趋势突破",
  sector_rotation: "板块轮动",
  dip_stabilization: "分歧低吸",
  oversold_reversal: "超跌反弹",
  cash_defense: "空仓防御",
  defensive_cash: "空仓防御",
  init: "初始化",
  "无": "观望等待",
};

const LIVE_SOURCES = new Set(["live", "realtime", "warehouse"]);

function pct(value: number | undefined, digits = 0): string {
  const n = Number(value ?? 0);
  return `${(n * 100).toFixed(digits)}%`;
}

function fixed(value: number | undefined, digits = 2): string {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n.toFixed(digits) : "--";
}

function cnStrategy(value: string): string {
  return STRATEGY_LABELS[value] || value || "观望等待";
}

function sourceLabel(source?: string): { text: string; color: string } {
  if (LIVE_SOURCES.has(source || "")) return { text: `真实行情 · ${source}`, color: "#22c55e" };
  if (!source || source === "init") return { text: "初始化/缓存快照 · 不出推荐", color: "#eab308" };
  return { text: `${source} · 不出推荐`, color: "#eab308" };
}

export default function AlphaTerminal() {
  const [data, setData] = useState<TerminalData | null>(null);
  const [report, setReport] = useState<CandidateReport | null>(null);
  const [marketFull, setMarketFull] = useState<MarketFullResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [terminalRes, candidateRes, marketRes] = await Promise.all([
        axios.get<TerminalData>("/api/alpha/terminal", { timeout: 8000 }),
        axios.get<CandidateReport>("/api/alpha/candidates?max=20", { timeout: 8000 }),
        axios.get<MarketFullResponse>("/api/market-full", { timeout: 8000 }),
      ]);
      setData(terminalRes.data);
      setReport(candidateRes.data);
      setMarketFull(marketRes.data);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Alpha Terminal 连接失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const timer = window.setInterval(fetchData, 30000);
    return () => window.clearInterval(timer);
  }, [fetchData]);

  const terminalLifecycle = data?.world_state?.lifecycles || {};
  const hasFullTerminalLifecycle = Object.values(terminalLifecycle).some((info: any) => (
    info && typeof info === "object" && "strength_score" in info
  ));
  const lifecycle = hasFullTerminalLifecycle ? terminalLifecycle : (marketFull?.lifecycles?.sectors || {});
  const phaseDistribution = data?.world_state?.sector_heatmap?.phase_distribution || marketFull?.lifecycles?.summary || {};
  const sectors = useMemo(() => {
    return Object.entries(lifecycle)
      .map(([name, info]) => ({ name, ...info }))
      .sort((a, b) => (b.strength_score || 0) - (a.strength_score || 0));
  }, [lifecycle]);

  const totalSectors = sectors.length || data?.world_state?.sector_heatmap?.total_sectors || 0;
  const activeCandidates = report?.candidates?.length ? report.candidates : data?.top_candidates || [];
  const source = sourceLabel(data?.data_source);

  if (loading) {
    return <TerminalShell><CenterText>正在连接 Alpha Terminal...</CenterText></TerminalShell>;
  }

  if (error || !data) {
    return <TerminalShell><CenterText color="#ef4444">{error || "Alpha Terminal 暂不可用"}</CenterText></TerminalShell>;
  }

  return (
    <TerminalShell>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", padding: "0.5rem 1rem", borderBottom: "1px solid #1f2937", background: "#0a0e17", flexWrap: "wrap" }}>
        <b style={{ color: "#e5e7eb" }}>ALPHA TERMINAL</b>
        <Dot color={source.color} />
        <span style={{ color: source.color }}>{source.text}</span>
        <span style={{ color: "#4b5563" }}>|</span>
        <span>{data.regime_display}</span>
        <span>轮动 {fixed(data.rotation_speed)}</span>
        <span>风险 {pct(data.risk_level)}</span>
        <span style={{ color: "#6b7280", marginLeft: "auto" }}>更新 {data.computed_at || data.ts}</span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "1rem", display: "grid", gridTemplateColumns: "1.15fr 1.4fr 1fr", gap: "0.75rem", alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <Panel title="市场认知">
            <Metric label="市场状态" value={data.regime_display} color="#eab308" />
            <Metric label="主策略" value={cnStrategy(data.primary_strategy)} color="#93c5fd" />
            <Metric label="市场偏向" value={fixed(data.market_bias)} color={data.market_bias >= 0.5 ? "#22c55e" : "#ef4444"} />
            <Metric label="动量评分" value={fixed(data.world_state?.momentum_score)} color="#a78bfa" />
            <Metric label="流动性" value={data.world_state?.liquidity_state || "--"} color="#9ca3af" />
            <Metric label="波动状态" value={data.world_state?.volatility_state || "--"} color="#9ca3af" />
          </Panel>

          <Panel title="生命周期覆盖">
            <div style={{ marginBottom: "0.5rem", color: "#d1d5db" }}>
              覆盖 <b style={{ color: "#e5e7eb" }}>{totalSectors}</b> 个板块，当前识别为六阶段模型。
            </div>
            {Object.entries(PHASE_LABELS)
              .filter(([phase]) => phase in phaseDistribution)
              .map(([phase, label]) => (
                <PhaseBar key={phase} label={label} count={phaseDistribution[phase] || 0} total={totalSectors} color={PHASE_COLORS[phase] || "#6b7280"} />
              ))}
          </Panel>

          <Panel title="激活策略">
            {(data.active_strategies || []).length === 0 ? (
              <Muted>无激活策略，等待行情数据恢复。</Muted>
            ) : (
              data.active_strategies.map((strategy) => (
                <Tag key={strategy} active={strategy === data.primary_strategy}>{cnStrategy(strategy)}</Tag>
              ))
            )}
          </Panel>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <Panel title="Alpha 摘要">
            <div style={{ color: "#facc15", marginBottom: "0.35rem" }}>{data.ai_explanation?.regime || data.ai_summary}</div>
            <TextLine>{data.ai_explanation?.rotation_analysis}</TextLine>
            <TextLine>{data.ai_explanation?.risk_assessment}</TextLine>
            <TextLine color="#93c5fd">{data.ai_explanation?.strategy_rationale}</TextLine>
            {(report?.explanation || data.ai_explanation?.warning) && (
              <div style={{ marginTop: "0.5rem", border: "1px solid rgba(234,179,8,0.35)", background: "rgba(234,179,8,0.08)", color: "#fde68a", padding: "0.5rem", borderRadius: "0.375rem" }}>
                {report?.explanation || data.ai_explanation?.warning}
              </div>
            )}
          </Panel>

          <Panel title="候选池">
            {activeCandidates.length === 0 ? (
              <div style={{ color: "#9ca3af", lineHeight: 1.7 }}>
                当前没有可发布候选。系统没有使用缓存或全 0 行情生成推荐，这是预期的风控行为。
              </div>
            ) : (
              <div style={{ display: "grid", gap: "0.35rem" }}>
                {activeCandidates.map((c) => (
                  <CandidateRow key={c.symbol} c={c} />
                ))}
              </div>
            )}
          </Panel>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <Panel title="强势板块">
            {sectors.slice(0, 8).map((s) => (
              <SectorRow key={s.name} sector={s} />
            ))}
          </Panel>

          <Panel title="市场事件">
            {(data.market_events || []).length === 0 ? (
              <Muted>暂无新增事件。若候选池为空，优先看数据源状态和生命周期覆盖。</Muted>
            ) : (
              data.market_events.slice(0, 6).map((event, index) => (
                <div key={`${event.event_type}-${index}`} style={{ borderBottom: "1px solid rgba(107,114,128,0.15)", padding: "0.4rem 0" }}>
                  <div style={{ color: event.severity === "critical" ? "#ef4444" : event.severity === "warning" ? "#eab308" : "#93c5fd", fontWeight: 700 }}>{event.event_type}</div>
                  <div style={{ color: "#9ca3af", lineHeight: 1.5 }}>{event.description}</div>
                </div>
              ))
            )}
          </Panel>
        </div>
      </div>
    </TerminalShell>
  );
}

function TerminalShell({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "#030712", color: "#d1d5db", fontFamily: "'SF Mono','Cascadia Code','Consolas',monospace", fontSize: "0.75rem", overflow: "hidden" }}>
      {children}
    </div>
  );
}

function CenterText({ children, color = "#9ca3af" }: { children: React.ReactNode; color?: string }) {
  return <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color }}>{children}</div>;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ border: "1px solid #1f2937", background: "#0a0e17", borderRadius: "0.5rem", padding: "0.75rem" }}>
      <div style={{ color: "#6b7280", fontSize: "0.68rem", fontWeight: 700, marginBottom: "0.55rem", letterSpacing: 0 }}>{title}</div>
      {children}
    </section>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", borderBottom: "1px solid rgba(107,114,128,0.12)", padding: "0.28rem 0" }}>
      <span style={{ color: "#9ca3af" }}>{label}</span>
      <b style={{ color }}>{value}</b>
    </div>
  );
}

function PhaseBar({ label, count, total, color }: { label: string; count: number; total: number; color: string }) {
  const width = total > 0 ? Math.max(4, Math.round((count / total) * 100)) : 0;
  return (
    <div style={{ marginBottom: "0.4rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.15rem" }}>
        <span>{label}</span>
        <span style={{ color }}>{count}</span>
      </div>
      <div style={{ height: "0.35rem", background: "#111827", borderRadius: "999px", overflow: "hidden" }}>
        <div style={{ width: `${width}%`, height: "100%", background: color }} />
      </div>
    </div>
  );
}

function SectorRow({ sector }: { sector: { name: string } & SectorInfo }) {
  const color = PHASE_COLORS[sector.phase] || "#6b7280";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.9fr 0.8fr", gap: "0.4rem", padding: "0.35rem 0", borderBottom: "1px solid rgba(107,114,128,0.12)" }}>
      <b style={{ color: "#e5e7eb", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sector.name}</b>
      <span style={{ color }}>{PHASE_LABELS[sector.phase] || sector.phase}</span>
      <span style={{ color: "#9ca3af", textAlign: "right" }}>{fixed(sector.strength_score, 1)}</span>
    </div>
  );
}

function CandidateRow({ c }: { c: Candidate }) {
  const tierColor = c.tier === "S" ? "#f59e0b" : c.tier === "A" ? "#22c55e" : c.tier === "B" ? "#3b82f6" : "#6b7280";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "2.2rem 1.2fr 0.8fr 0.7fr 0.7fr", gap: "0.4rem", alignItems: "center", border: "1px solid rgba(107,114,128,0.18)", borderRadius: "0.375rem", padding: "0.45rem" }}>
      <b style={{ color: tierColor }}>{c.tier || "W"}</b>
      <div>
        <b style={{ color: "#e5e7eb" }}>{c.name}</b>
        <span style={{ color: "#6b7280", marginLeft: "0.35rem" }}>{c.symbol}</span>
      </div>
      <span style={{ color: "#9ca3af" }}>{c.sector || "--"}</span>
      <span style={{ color: "#facc15" }}>{fixed(c.score, 1)}</span>
      <span style={{ color: c.risk_reward >= 2 ? "#22c55e" : "#9ca3af" }}>{fixed(c.risk_reward, 1)}</span>
    </div>
  );
}

function Tag({ children, active }: { children: React.ReactNode; active?: boolean }) {
  return (
    <span style={{ display: "inline-flex", margin: "0 0.35rem 0.35rem 0", padding: "0.25rem 0.5rem", borderRadius: "0.375rem", border: active ? "1px solid rgba(59,130,246,0.45)" : "1px solid #1f2937", background: active ? "rgba(59,130,246,0.12)" : "#111827", color: active ? "#93c5fd" : "#9ca3af" }}>
      {children}
    </span>
  );
}

function Dot({ color }: { color: string }) {
  return <span style={{ width: "0.5rem", height: "0.5rem", borderRadius: "50%", background: color, display: "inline-block" }} />;
}

function TextLine({ children, color = "#9ca3af" }: { children?: string; color?: string }) {
  if (!children) return null;
  return <div style={{ color, lineHeight: 1.7 }}>{children}</div>;
}

function Muted({ children }: { children: React.ReactNode }) {
  return <div style={{ color: "#6b7280", lineHeight: 1.6 }}>{children}</div>;
}
