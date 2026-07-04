import ReactECharts from "echarts-for-react";

export interface OHLCVItem {
  date: string;
  open: number;
  close: number;
  low: number;
  high: number;
  volume: number;
}

interface StockChartProps {
  data: OHLCVItem[];
  title?: string;
  period?: "day" | "week" | "month" | "fiveDay";
  dataSource?: "live" | "mock";
}

function sma(values: number[], period: number): (number | null)[] {
  return values.map((_, index) => {
    if (index < period - 1) return null;
    const window = values.slice(index - period + 1, index + 1);
    return window.reduce((sum, value) => sum + value, 0) / period;
  });
}

function ema(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period) return result;
  const k = 2 / (period + 1);
  let prev = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  result[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i]! * k + prev * (1 - k);
    result[i] = prev;
  }
  return result;
}

function calcMACD(values: number[]) {
  const fast = ema(values, 12);
  const slow = ema(values, 26);
  const dif = values.map((_, i) => fast[i] != null && slow[i] != null ? fast[i]! - slow[i]! : null);
  const valid = dif.filter((value): value is number => value != null);
  const deaValid = ema(valid, 9);
  const dea: (number | null)[] = new Array(values.length).fill(null);
  let cursor = 0;
  dif.forEach((value, index) => {
    if (value == null) return;
    dea[index] = deaValid[cursor] ?? null;
    cursor += 1;
  });
  const macd = dif.map((value, index) => value != null && dea[index] != null ? (value - dea[index]!) * 2 : null);
  return { dif, dea, macd };
}

function fmt(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? "--" : value.toFixed(digits);
}

export default function StockChart({ data, title, period = "day", dataSource = "live" }: StockChartProps) {
  const isMock = dataSource === "mock";
  const isFiveDay = period === "fiveDay";
  const dates = data.map((item) => item.date);
  const closes = data.map((item) => item.close);
  const volumes = data.map((item) => item.volume);
  const periodLabel = period === "week" ? "周线" : period === "month" ? "月线" : isFiveDay ? "五日" : "日线";
  const defaultZoomStart = isFiveDay ? 0 : period === "month" ? Math.max(0, 100 - Math.round(72 / Math.max(1, data.length) * 100)) : period === "week" ? Math.max(0, 100 - Math.round(130 / Math.max(1, data.length) * 100)) : Math.max(0, 100 - Math.round(150 / Math.max(1, data.length) * 100));

  const ma5 = sma(closes, 5);
  const ma10 = sma(closes, 10);
  const ma20 = sma(closes, 20);
  const ma30 = sma(closes, 30);
  const ma60 = sma(closes, 60);
  const volMa5 = sma(volumes, 5);
  const volMa10 = sma(volumes, 10);
  const { dif, dea, macd } = calcMACD(closes);

  const lastIndex = Math.max(0, data.length - 1);
  const lastClose = closes[lastIndex] ?? 0;
  const prevClose = closes[Math.max(0, lastIndex - 1)] ?? lastClose;
  const changePct = prevClose > 0 ? (lastClose - prevClose) / prevClose * 100 : 0;
  const lastVol = volumes[lastIndex] ?? 0;
  const avgVol = volumes.slice(-20).reduce((sum, value) => sum + value, 0) / Math.max(1, Math.min(20, volumes.length));
  const volumeRatio = avgVol > 0 ? lastVol / avgVol : 1;

  const fullGrids = [
    { left: 58, right: 28, top: 34, height: 340 },
    { left: 58, right: 28, top: 398, height: 130 },
    { left: 58, right: 28, top: 555, height: 145 },
  ];
  const compactGrids = [
    { left: 58, right: 28, top: 34, height: 360 },
    { left: 58, right: 28, top: 420, height: 145 },
  ];
  const grids = isFiveDay ? compactGrids : fullGrids;
  const chartHeight = isFiveDay ? 640 : 770;
  const candleData = data.map((item) => [item.open, item.close, item.low, item.high]);
  const upColor = "#ef4444";
  const downColor = "#00b8b8";

  const option: any = {
    animation: false,
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
      backgroundColor: "rgba(15,23,42,0.96)",
      borderColor: "#334155",
      textStyle: { color: "#e5e7eb", fontSize: 12 },
    },
    legend: {
      top: 2,
      left: 8,
      itemWidth: 16,
      itemHeight: 2,
      textStyle: { color: "#cbd5e1", fontSize: 12 },
      data: isFiveDay
        ? ["K线", "成交量"]
        : ["K线", "MA5", "MA10", "MA20", "MA30", "MA60", "成交量", "VOL-MA5", "VOL-MA10", "MACD", "DIF", "DEA"],
    },
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: isFiveDay ? [0, 1] : [0, 1, 2],
        start: defaultZoomStart,
        end: 100,
        zoomOnMouseWheel: false,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        preventDefaultMouseMove: false,
      },
      {
        type: "slider",
        xAxisIndex: isFiveDay ? [0, 1] : [0, 1, 2],
        start: defaultZoomStart,
        end: 100,
        height: 18,
        bottom: 8,
        borderColor: "#334155",
        fillerColor: "rgba(59,130,246,0.18)",
        handleStyle: { color: "#64748b" },
        textStyle: { color: "#94a3b8" },
        brushSelect: false,
      },
    ],
    grid: grids,
    xAxis: grids.map((_, index) => ({
      type: "category",
      data: dates,
      gridIndex: index,
      boundaryGap: true,
      axisLine: { lineStyle: { color: "#334155" } },
      axisTick: { show: false },
      axisLabel: index === grids.length - 1 ? { color: "#94a3b8", fontSize: 11 } : { show: false },
      splitLine: { show: false },
    })),
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        position: "left",
        axisLabel: { color: "#f87171", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1f2937" } },
      },
      {
        scale: true,
        gridIndex: 1,
        position: "left",
        axisLabel: { color: "#94a3b8", fontSize: 11, formatter: (value: number) => value >= 10000 ? `${(value / 10000).toFixed(0)}万` : String(Math.round(value)) },
        splitLine: { lineStyle: { color: "#1f2937" } },
      },
      ...(!isFiveDay ? [{
        scale: true,
        gridIndex: 2,
        position: "left",
        axisLabel: { color: "#94a3b8", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1f2937" } },
      }] : []),
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: candleData,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
      },
      ...(!isFiveDay ? [
        { name: "MA5", type: "line", data: ma5, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: "#facc15", width: 1.2 } },
        { name: "MA10", type: "line", data: ma10, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: "#f59e0b", width: 1.2 } },
        { name: "MA20", type: "line", data: ma20, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: "#ec4899", width: 1.2 } },
        { name: "MA30", type: "line", data: ma30, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: "#22c55e", width: 1.2 } },
        { name: "MA60", type: "line", data: ma60, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { color: "#0ea5e9", width: 1.2 } },
      ] : []),
      {
        name: "成交量",
        type: "bar",
        data: volumes.map((value, index) => ({
          value,
          itemStyle: { color: data[index]!.close >= data[index]!.open ? upColor : downColor },
        })),
        xAxisIndex: 1,
        yAxisIndex: 1,
        barWidth: isFiveDay ? "42%" : "58%",
      },
      ...(!isFiveDay ? [
        { name: "VOL-MA5", type: "line", data: volMa5, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { color: "#facc15", width: 1 } },
        { name: "VOL-MA10", type: "line", data: volMa10, xAxisIndex: 1, yAxisIndex: 1, symbol: "none", lineStyle: { color: "#cbd5e1", width: 1 } },
        {
          name: "MACD",
          type: "bar",
          data: macd.map((value) => ({ value, itemStyle: { color: (value ?? 0) >= 0 ? upColor : downColor } })),
          xAxisIndex: 2,
          yAxisIndex: 2,
          barWidth: "48%",
        },
        { name: "DIF", type: "line", data: dif, xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { color: "#e5e7eb", width: 1.1 } },
        { name: "DEA", type: "line", data: dea, xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { color: "#facc15", width: 1.1 } },
      ] : []),
    ],
  };

  const statusText = isFiveDay
    ? `最近5个交易日（前五日） · ${title || ""}`
    : `${periodLabel} · MA5 ${fmt(ma5[lastIndex])} · MA10 ${fmt(ma10[lastIndex])} · MA20 ${fmt(ma20[lastIndex])} · MA30 ${fmt(ma30[lastIndex])} · MA60 ${fmt(ma60[lastIndex])}`;

  return (
    <div style={{ border: "1px solid #1f2937", background: "#0b0f19", height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ minHeight: 34, display: "flex", alignItems: "center", gap: 8, padding: "0 10px", borderBottom: "1px solid #1f2937", color: "#cbd5e1", fontSize: 12, flexWrap: "wrap" }}>
        {isMock && <span style={{ color: "#facc15", fontWeight: 700 }}>模拟K线 · 仅展示</span>}
        <b style={{ color: changePct >= 0 ? upColor : downColor }}>{periodLabel}</b>
        <span>{statusText}</span>
        <span style={{ marginLeft: "auto", color: volumeRatio >= 1.5 ? "#f87171" : "#94a3b8" }}>量比 {volumeRatio.toFixed(2)}</span>
      </div>
      <div style={{ height: chartHeight, minHeight: chartHeight }}>
        <ReactECharts option={option} style={{ height: "100%", width: "100%" }} notMerge lazyUpdate />
      </div>
      {isMock && (
        <div style={{ margin: "0 10px 10px", border: "1px solid rgba(251,191,36,0.35)", background: "rgba(251,191,36,0.06)", padding: "8px 10px", color: "#fcd34d", fontSize: 12 }}>
          当前图表使用确定性模拟数据，仅用于空状态占位展示；不会进入评分、推荐或筛选逻辑。
        </div>
      )}
    </div>
  );
}

export function generateMockData(days = 520): OHLCVItem[] {
  const data: OHLCVItem[] = [];
  let seed = 20260704;
  const nextRand = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 0x100000000;
  };
  let price = 25 + nextRand() * 10;
  const now = new Date();
  for (let i = days; i >= 0; i--) {
    const change = (nextRand() - 0.48) * 1.2;
    const open = price;
    const close = Math.max(0.1, price + change);
    const high = Math.max(open, close) + nextRand() * 0.8;
    const low = Math.max(0.1, Math.min(open, close) - nextRand() * 0.8);
    const volume = Math.floor((1 + nextRand()) * 5_000_000);
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    data.push({ date: `${y}-${m}-${dd}`, open, close, low, high, volume });
    price = close;
  }
  return data;
}
