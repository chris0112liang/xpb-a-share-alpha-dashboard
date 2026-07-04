import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import axios from "axios";

type Mode = "balanced" | "trend" | "pullback" | "defensive";

interface SectorLifecycle {
  phase: string;
  confidence: number;
  bias: number;
  strength_score: number;
  price_mom_5?: number;
  price_mom_20?: number;
  vol_trend?: number;
  is_turning?: boolean;
  data_rows?: number;
  updated_at?: string;
}

interface SectorRegistryItem {
  name: string;
  stocks: string[];
  stock_cnt: number;
  board_type: string;
  is_anchor?: boolean;
}

interface Candidate {
  symbol: string;
  name: string;
  sector: string;
  score: number;
  tier: string;
  confidence: number;
  risk_reward: number;
  sector_phase_cn?: string;
  triggered_strategy_display?: string;
  reasons?: string[];
}

interface CandidateReport {
  candidates: Candidate[];
  total_candidates: number;
  primary_strategy: string;
  explanation: string;
}

interface StrategyScreenerProps {
  onSelectStock?: (code: string) => void;
}

const PHASES = [
  { id: "acceleration", label: "加速期", desc: "强趋势，但追高风险最高" },
  { id: "main_rise_1", label: "主升一期", desc: "趋势最稳，适合顺势" },
  { id: "startup", label: "启动期", desc: "资金试探，适合观察确认" },
  { id: "ice_recovery", label: "冰点修复", desc: "超跌企稳，适合左侧小仓" },
  { id: "high_divergence", label: "高位分歧", desc: "止盈优先，谨慎接力" },
  { id: "decay", label: "退潮期", desc: "系统性回避" },
  { id: "noise", label: "横盘震荡", desc: "信号弱，等待方向" },
];

const PHASE_LABELS = Object.fromEntries(PHASES.map((p) => [p.id, p.label]));
const PHASE_COLORS: Record<string, string> = {
  acceleration: "#f97316",
  main_rise_1: "#eab308",
  startup: "#22c55e",
  ice_recovery: "#06b6d4",
  high_divergence: "#fb923c",
  decay: "#ef4444",
  noise: "#6b7280",
};

const MODES: Array<{ id: Mode; label: string; desc: string }> = [
  { id: "balanced", label: "均衡", desc: "强度、阶段、风险综合" },
  { id: "trend", label: "趋势", desc: "主升/加速优先" },
  { id: "pullback", label: "低吸", desc: "启动/冰点修复优先" },
  { id: "defensive", label: "防守", desc: "排除高位和退潮" },
];

const HOT_GROUPS = [
  { id: "ai", label: "AI/算力", match: ["AI", "算力", "CPO", "半导体", "芯片"] },
  { id: "robot", label: "机器人/智能驾驶", match: ["机器人", "智能驾驶", "汽车"] },
  { id: "energy", label: "新能源/储能", match: ["新能源", "储能", "光伏", "锂电"] },
  { id: "finance", label: "金融/红利", match: ["银行", "证券", "保险", "煤炭"] },
  { id: "consumer", label: "消费/医药", match: ["消费", "白酒", "医药", "家电"] },
  { id: "defense", label: "军工/低空", match: ["军工", "低空", "航空"] },
];

function phaseScore(phase: string, mode: Mode): number {
  const balanced: Record<string, number> = {
    main_rise_1: 28,
    acceleration: 22,
    startup: 20,
    ice_recovery: 14,
    high_divergence: -10,
    decay: -30,
    noise: -8,
  };
  const trend: Record<string, number> = {
    acceleration: 30,
    main_rise_1: 28,
    startup: 10,
    high_divergence: -15,
    ice_recovery: -4,
    decay: -35,
    noise: -10,
  };
  const pullback: Record<string, number> = {
    ice_recovery: 28,
    startup: 25,
    main_rise_1: 12,
    high_divergence: 4,
    acceleration: -8,
    decay: -20,
    noise: -6,
  };
  const defensive: Record<string, number> = {
    main_rise_1: 20,
    startup: 16,
    ice_recovery: 10,
    acceleration: 4,
    high_divergence: -25,
    decay: -40,
    noise: -8,
  };
  return ({ balanced, trend, pullback, defensive }[mode][phase] ?? 0);
}

function fixed(value: number | undefined, digits = 1): string {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n.toFixed(digits) : "--";
}

function riskLabel(phase: string, turning?: boolean): { text: string; color: string } {
  if (phase === "decay") return { text: "回避", color: "#ef4444" };
  if (phase === "high_divergence" || turning) return { text: "止盈/轻仓", color: "#fb923c" };
  if (phase === "acceleration") return { text: "高波动", color: "#eab308" };
  if (phase === "main_rise_1") return { text: "顺势", color: "#22c55e" };
  if (phase === "startup") return { text: "观察确认", color: "#22d3ee" };
  if (phase === "ice_recovery") return { text: "左侧小仓", color: "#06b6d4" };
  return { text: "等待", color: "#9ca3af" };
}

export default function StrategyScreener({ onSelectStock }: StrategyScreenerProps) {
  const [mode, setMode] = useState<Mode>("balanced");
  const [selectedPhases, setSelectedPhases] = useState<string[]>(["main_rise_1", "startup", "acceleration"]);
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [lifecycles, setLifecycles] = useState<Record<string, SectorLifecycle>>({});
  const [registry, setRegistry] = useState<Record<string, SectorRegistryItem>>({});
  const [candidateReport, setCandidateReport] = useState<CandidateReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [marketRes, registryRes, candidateRes] = await Promise.all([
        axios.get("/api/market-full", { timeout: 8000 }),
        axios.get("/api/sector/registry", { timeout: 8000 }),
        axios.get<CandidateReport>("/api/alpha/candidates?max=30", { timeout: 8000 }),
      ]);
      setLifecycles(marketRes.data?.lifecycles?.sectors || {});
      const byName: Record<string, SectorRegistryItem> = {};
      for (const item of registryRes.data?.sectors || []) byName[item.name] = item;
      setRegistry(byName);
      setCandidateReport(candidateRes.data);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "策略数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const timer = window.setInterval(fetchData, 60000);
    return () => window.clearInterval(timer);
  }, [fetchData]);

  const selectedMatchers = useMemo(() => {
    return HOT_GROUPS.filter((g) => selectedGroups.includes(g.id)).flatMap((g) => g.match);
  }, [selectedGroups]);

  const sectors = useMemo(() => {
    return Object.entries(lifecycles)
      .map(([name, info]) => {
        const reg = registry[name];
        const groupHit = selectedMatchers.length === 0 || selectedMatchers.some((m) => name.includes(m));
        const phaseHit = selectedPhases.length === 0 || selectedPhases.includes(info.phase);
        const riskPenalty = info.is_turning ? 12 : 0;
        const score = Math.max(0, Math.min(100,
          (info.strength_score || 0) * 0.62 +
          phaseScore(info.phase, mode) +
          (info.confidence || 0) * 18 -
          riskPenalty
        ));
        return {
          name,
          ...info,
          registry: reg,
          visible: groupHit && phaseHit,
          strategyScore: Math.round(score),
        };
      })
      .filter((s) => s.visible)
      .sort((a, b) => b.strategyScore - a.strategyScore);
  }, [lifecycles, mode, registry, selectedMatchers, selectedPhases]);

  const candidates = useMemo(() => {
    const list = candidateReport?.candidates || [];
    if (selectedPhases.length === 0 && selectedGroups.length === 0) return list;
    return list.filter((c) => {
      const sector = lifecycles[c.sector];
      const phaseOk = selectedPhases.length === 0 || selectedPhases.includes(sector?.phase || "");
      const groupOk = selectedMatchers.length === 0 || selectedMatchers.some((m) => c.sector?.includes(m));
      return phaseOk && groupOk;
    });
  }, [candidateReport, lifecycles, selectedGroups.length, selectedMatchers, selectedPhases]);

  const phaseDist = useMemo(() => {
    const dist: Record<string, number> = {};
    for (const info of Object.values(lifecycles)) dist[info.phase] = (dist[info.phase] || 0) + 1;
    return dist;
  }, [lifecycles]);

  const toggle = (list: string[], id: string) => list.includes(id) ? list.filter((x) => x !== id) : [...list, id];

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateColumns: "19rem 1fr", background: "#030712", color: "#e5e7eb", overflow: "hidden" }}>
      <aside style={{ borderRight: "1px solid #1f2937", padding: "1rem", overflowY: "auto" }}>
        <Panel title="策略模式">
          <div style={{ display: "grid", gap: "0.4rem" }}>
            {MODES.map((m) => (
              <button key={m.id} onClick={() => setMode(m.id)} style={buttonStyle(mode === m.id)}>
                <b>{m.label}</b>
                <span>{m.desc}</span>
              </button>
            ))}
          </div>
        </Panel>

        <Panel title="生命周期阶段">
          {PHASES.map((phase) => (
            <button key={phase.id} onClick={() => setSelectedPhases(toggle(selectedPhases, phase.id))} style={filterButtonStyle(selectedPhases.includes(phase.id), PHASE_COLORS[phase.id])}>
              <span>
                <b>{phase.label}</b>
                <small>{phase.desc}</small>
              </span>
              <em>{phaseDist[phase.id] || 0}</em>
            </button>
          ))}
        </Panel>

        <Panel title="主题赛道">
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
            {HOT_GROUPS.map((group) => (
              <button key={group.id} onClick={() => setSelectedGroups(toggle(selectedGroups, group.id))} style={chipStyle(selectedGroups.includes(group.id))}>
                {group.label}
              </button>
            ))}
          </div>
        </Panel>

        <button onClick={() => { setSelectedGroups([]); setSelectedPhases([]); }} style={{ width: "100%", marginTop: "0.5rem", padding: "0.55rem", border: "1px solid #374151", borderRadius: "0.375rem", background: "transparent", color: "#9ca3af", cursor: "pointer" }}>
          清空筛选
        </button>
      </aside>

      <main style={{ overflow: "auto", padding: "1rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", alignItems: "flex-start", marginBottom: "0.75rem" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: "1rem" }}>策略选股工作台</h2>
            <p style={{ margin: "0.35rem 0 0", color: "#9ca3af", fontSize: "0.75rem" }}>
              先根据板块生命周期筛出可操作方向；只有真实行情通过风控时，才展示个股候选。
            </p>
          </div>
          <div style={{ textAlign: "right", color: "#9ca3af", fontSize: "0.75rem" }}>
            <div>匹配板块 <b style={{ color: "#e5e7eb" }}>{sectors.length}</b></div>
            <div>真实候选 <b style={{ color: candidates.length ? "#22c55e" : "#eab308" }}>{candidates.length}</b></div>
          </div>
        </div>

        {error && <Notice color="#ef4444">{error}</Notice>}
        {!error && candidateReport?.explanation && <Notice color="#eab308">{candidateReport.explanation}</Notice>}

        {loading ? (
          <div style={{ color: "#9ca3af", padding: "3rem", textAlign: "center" }}>正在加载生命周期和候选池...</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.9fr", gap: "0.75rem", alignItems: "start" }}>
            <section style={{ display: "grid", gap: "0.55rem" }}>
              {sectors.map((sector) => (
                <SectorCard key={sector.name} sector={sector} onSelectStock={onSelectStock} />
              ))}
              {sectors.length === 0 && (
                <Empty title="没有匹配板块" text="放宽生命周期阶段或主题赛道筛选后再看。" />
              )}
            </section>

            <aside style={{ display: "grid", gap: "0.75rem" }}>
              <Panel title="真实 Alpha 候选">
                {candidates.length === 0 ? (
                  <div style={{ color: "#9ca3af", lineHeight: 1.7 }}>
                    当前候选池为空。系统没有用缓存或板块映射硬凑个股推荐，这是简历项目里更重要的可信边界。
                  </div>
                ) : (
                  <div style={{ display: "grid", gap: "0.45rem" }}>
                    {candidates.map((c) => (
                      <CandidateCard key={c.symbol} c={c} onSelectStock={onSelectStock} />
                    ))}
                  </div>
                )}
              </Panel>

              <Panel title="阶段解释">
                <ExplainLine phase="main_rise_1" text="主升一期：优先级最高，适合顺势跟踪，仍需看量能和风险收益比。" />
                <ExplainLine phase="acceleration" text="加速期：强度高但波动大，适合已有仓位管理，不适合无脑追。" />
                <ExplainLine phase="startup" text="启动期：适合观察首板块共振和资金确认。" />
                <ExplainLine phase="ice_recovery" text="冰点修复：可看反弹窗口，但仓位和止损要更严格。" />
                <ExplainLine phase="high_divergence" text="高位分歧：以止盈和降风险为主。" />
                <ExplainLine phase="decay" text="退潮期：默认回避。" />
              </Panel>
            </aside>
          </div>
        )}
      </main>
    </div>
  );
}

function SectorCard({ sector, onSelectStock }: { sector: any; onSelectStock?: (code: string) => void }) {
  const risk = riskLabel(sector.phase, sector.is_turning);
  const phaseColor = PHASE_COLORS[sector.phase] || "#6b7280";
  const sampleStocks = sector.registry?.stocks?.slice(0, 8) || [];
  return (
    <div style={{ border: "1px solid #1f2937", background: "#0a0e17", borderRadius: "0.5rem", padding: "0.75rem" }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "1rem", alignItems: "start" }}>
        <div>
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
            <b style={{ fontSize: "0.95rem" }}>{sector.name}</b>
            <span style={badgeStyle(phaseColor)}>{PHASE_LABELS[sector.phase] || sector.phase}</span>
            <span style={badgeStyle(risk.color)}>{risk.text}</span>
            {sector.is_turning && <span style={badgeStyle("#ef4444")}>拐点预警</span>}
          </div>
          <div style={{ color: "#9ca3af", fontSize: "0.72rem", marginTop: "0.4rem" }}>
            5日动量 {fixed(sector.price_mom_5)}% · 20日动量 {fixed(sector.price_mom_20)}% · 量能趋势 {fixed(sector.vol_trend)}% · 样本 {sector.data_rows || 0} 日
          </div>
        </div>
        <Score score={sector.strategyScore} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: "0.5rem", marginTop: "0.75rem" }}>
        <Mini label="强度" value={fixed(sector.strength_score)} />
        <Mini label="置信度" value={`${fixed((sector.confidence || 0) * 100, 0)}%`} />
        <Mini label="乖离" value={`${fixed(sector.bias)}%`} />
        <Mini label="股票池" value={`${sector.registry?.stock_cnt || sampleStocks.length || 0}只`} />
      </div>

      {sampleStocks.length > 0 && (
        <div style={{ marginTop: "0.65rem", display: "flex", flexWrap: "wrap", gap: "0.35rem", alignItems: "center" }}>
          <span style={{ color: "#6b7280", fontSize: "0.68rem" }}>映射股票池样本，非推荐：</span>
          {sampleStocks.map((code: string) => (
            <button key={code} onClick={() => onSelectStock?.(code)} style={{ border: "1px solid #374151", background: "#111827", color: "#9ca3af", borderRadius: "0.25rem", padding: "0.15rem 0.38rem", cursor: "pointer", fontSize: "0.68rem" }}>
              {code}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CandidateCard({ c, onSelectStock }: { c: Candidate; onSelectStock?: (code: string) => void }) {
  return (
    <button onClick={() => onSelectStock?.(c.symbol)} style={{ textAlign: "left", border: "1px solid #1f2937", background: "#111827", color: "#d1d5db", borderRadius: "0.4rem", padding: "0.55rem", cursor: "pointer" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
        <b>{c.name} <span style={{ color: "#6b7280" }}>{c.symbol}</span></b>
        <span style={{ color: "#facc15" }}>{fixed(c.score)}</span>
      </div>
      <div style={{ color: "#9ca3af", fontSize: "0.7rem", marginTop: "0.25rem" }}>
        {c.sector} · {c.sector_phase_cn || "--"} · {c.triggered_strategy_display || "--"} · 赔率 {fixed(c.risk_reward)}
      </div>
    </button>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section style={{ border: "1px solid #1f2937", background: "#0a0e17", borderRadius: "0.5rem", padding: "0.75rem", marginBottom: "0.75rem" }}>
      <div style={{ color: "#9ca3af", fontWeight: 700, fontSize: "0.75rem", marginBottom: "0.6rem" }}>{title}</div>
      {children}
    </section>
  );
}

function Score({ score }: { score: number }) {
  const color = score >= 75 ? "#22c55e" : score >= 60 ? "#eab308" : score >= 45 ? "#9ca3af" : "#ef4444";
  return (
    <div style={{ width: "3rem", height: "3rem", borderRadius: "50%", background: `${color}22`, color, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800 }}>
      {score}
    </div>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ background: "#111827", border: "1px solid rgba(107,114,128,0.18)", borderRadius: "0.375rem", padding: "0.45rem" }}>
      <div style={{ color: "#6b7280", fontSize: "0.65rem" }}>{label}</div>
      <b style={{ color: "#e5e7eb", fontSize: "0.78rem" }}>{value}</b>
    </div>
  );
}

function Notice({ children, color }: { children: ReactNode; color: string }) {
  return (
    <div style={{ border: `1px solid ${color}55`, background: `${color}12`, color, borderRadius: "0.45rem", padding: "0.6rem 0.75rem", marginBottom: "0.75rem", fontSize: "0.78rem" }}>
      {children}
    </div>
  );
}

function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div style={{ border: "1px dashed #374151", borderRadius: "0.5rem", padding: "3rem", textAlign: "center", color: "#9ca3af" }}>
      <b>{title}</b>
      <div style={{ marginTop: "0.35rem" }}>{text}</div>
    </div>
  );
}

function ExplainLine({ phase, text }: { phase: string; text: string }) {
  return (
    <div style={{ color: "#9ca3af", lineHeight: 1.6, marginBottom: "0.45rem" }}>
      <span style={{ color: PHASE_COLORS[phase], fontWeight: 700 }}>{PHASE_LABELS[phase]}</span>：{text}
    </div>
  );
}

function buttonStyle(active: boolean): React.CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    gap: "0.15rem",
    border: active ? "1px solid rgba(59,130,246,0.55)" : "1px solid #1f2937",
    background: active ? "rgba(59,130,246,0.12)" : "#111827",
    color: active ? "#93c5fd" : "#d1d5db",
    borderRadius: "0.4rem",
    padding: "0.5rem",
    cursor: "pointer",
  };
}

function filterButtonStyle(active: boolean, color = "#6b7280"): React.CSSProperties {
  return {
    width: "100%",
    display: "flex",
    justifyContent: "space-between",
    gap: "0.5rem",
    border: active ? `1px solid ${color}88` : "1px solid #1f2937",
    background: active ? `${color}18` : "#111827",
    color: active ? "#e5e7eb" : "#9ca3af",
    borderRadius: "0.4rem",
    padding: "0.5rem",
    marginBottom: "0.4rem",
    cursor: "pointer",
    textAlign: "left",
  };
}

function chipStyle(active: boolean): React.CSSProperties {
  return {
    border: active ? "1px solid rgba(34,211,238,0.55)" : "1px solid #1f2937",
    background: active ? "rgba(34,211,238,0.12)" : "#111827",
    color: active ? "#67e8f9" : "#9ca3af",
    borderRadius: "999px",
    padding: "0.35rem 0.65rem",
    cursor: "pointer",
    fontSize: "0.72rem",
  };
}

function badgeStyle(color: string): React.CSSProperties {
  return {
    border: `1px solid ${color}66`,
    background: `${color}18`,
    color,
    borderRadius: "0.25rem",
    padding: "0.12rem 0.4rem",
    fontSize: "0.68rem",
    fontWeight: 700,
  };
}
