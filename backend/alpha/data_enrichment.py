"""
alpha/data_enrichment.py — 真实个股数据增强层

职责：
  在 Candidate Ranking 之前，为每只候选股补全真实数据。

数据来源优先级（由高到低）：
  1. Warehouse Parquet 日K (高速本地)
  2. 实时 MarketSnapshot (AKShare/Sina)
  3. 合理 fallback 估值（标记来源质量）

输出：EnrichedStockSnapshot — 包含 ranking_v2 所需的全部字段
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from databus.warehouse import get_warehouse
from schemas import MarketBar

logger = logging.getLogger(__name__)

# ── 常量 ──

# 数据源质量标签
SOURCE_WAREHOUSE = "warehouse"      # Parquet 优化数据
SOURCE_REALTIME = "realtime"        # 实时行情快照
SOURCE_FALLBACK = "fallback"        # 合理估测

# ATR 周期
ATR_PERIOD = 14
RS_WINDOW = 20  # RS相对强度窗口
RS_REF_INDEX = "000300"  # 基准指数


class EnrichedStockSnapshot:
    """增强个股快照——ranking_v2 的输入

    所有字段都有明确来源标记。
    """

    def __init__(
        self,
        symbol: str,
        name: str = "",
        price: float = 0.0,
        change_pct: float = 0.0,
        volume: float = 0.0,
        amount: float = 0.0,
        turnover_pct: float = 0.0,
    ):
        self.symbol = symbol
        self.name = name
        self.price = price
        self.change_pct = change_pct
        self.volume = volume
        self.amount = amount
        self.turnover_pct = turnover_pct

        # ── 将被填充 ──
        self.pct_5d: float = 0.0
        self.pct_10d: float = 0.0
        self.volume_ratio: float = 1.0
        self.atr: float = 0.0          # 14日 ATR（价格单位）
        self.atr_pct: float = 0.0      # ATR 百分比
        self.volatility: float = 0.0   # 20日年化波动率
        self.relative_strength: float = 0.0  # [-100, 100]
        self.liquidity_score: float = 0.0    # [0, 1]
        self.drawdown_pct: float = 0.0       # 近20日最大回撤 %
        self.max_high_20d: float = 0.0
        self.min_low_20d: float = 0.0
        self.avg_volume_20: float = 0.0  # 20日均量
        self.avg_volume_5: float = 0.0   # 5日均量
        self.sector_rank: int = 9999     # 板块内排名（由 scanner 填充）

        # 元数据
        self.data_source: str = SOURCE_FALLBACK
        self.has_warehouse_data: bool = False
        self.bars_5d: list[MarketBar] = []
        self.bars_10d: list[MarketBar] = []
        self.bars_20d: list[MarketBar] = []

    def to_dict(self) -> dict:
        """输出兼容 scanner / AlphaCandidate 的字段名"""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": round(self.price, 2),
            "change_pct": round(self.change_pct, 2),
            "change_pct_5d": round(self.pct_5d, 2),      # AlphaCandidate 字段名
            "pct_5d": round(self.pct_5d, 2),              # ranking_v2 extra 字段名
            "change_pct_10d": round(self.pct_10d, 2),
            "pct_10d": round(self.pct_10d, 2),
            "volume_ratio": round(self.volume_ratio, 2),
            "turnover_pct": round(self.turnover_pct, 2),   # ranking_v2 extra
            "turnover_rate": round(self.turnover_pct, 2),  # AlphaCandidate
            "atr": round(self.atr, 4),
            "atr_pct": round(self.atr_pct, 4),
            "volatility": round(self.volatility, 4),
            "relative_strength": round(self.relative_strength, 1),
            "liquidity_score": round(self.liquidity_score, 2),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "data_source": self.data_source,
        }


# ── 预热缓存 ──

_WAREHOUSE_CACHE: dict[str, list[MarketBar]] = {}


def _load_cached_bars(symbol: str, days: int = 65) -> list[MarketBar]:
    """从 Warehouse 加载日K（带进程内 LRU）"""
    if symbol in _WAREHOUSE_CACHE:
        return _WAREHOUSE_CACHE[symbol][-days:]

    try:
        wh = get_warehouse()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
        bars = wh.query_daily_bars(symbol, start_date=start, end_date=end)
        if bars:
            _WAREHOUSE_CACHE[symbol] = bars
            return bars[-days:]
    except Exception as e:
        logger.debug(f"[DataEnrich] Warehouse query failed for {symbol}: {e}")

    return []


def _compute_volume_ratio(volume: float, avg_volume_5: float, avg_volume_20: float) -> float:
    """量比——当日量 / 5日均量"""
    if avg_volume_5 <= 0:
        return 1.0
    raw = volume / avg_volume_5
    # 截断到合理范围
    return max(0.1, min(raw, 10.0))


def _compute_atr(bars: list[MarketBar], period: int = ATR_PERIOD) -> tuple[float, float]:
    """计算 ATR（价格单位 + 百分比）"""
    if len(bars) < period + 1:
        return 0.0, 0.0

    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        prev_close = bars[i - 1].close
        high = bars[i].high
        low = bars[i].low
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_sum += tr

    atr = tr_sum / period
    current_price = bars[-1].close
    atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0
    return atr, atr_pct


def _compute_volatility(bars: list[MarketBar], period: int = 20) -> float:
    """20日年化波动率（基于日收益率）"""
    if len(bars) < period:
        return 0.0

    returns = []
    for i in range(len(bars) - period + 1, len(bars)):
        prev = bars[i - 1].close
        curr = bars[i].close
        if prev > 0:
            returns.append((curr - prev) / prev)

    if len(returns) < 2:
        return 0.0

    import statistics
    std = statistics.stdev(returns)
    return std * math.sqrt(252)  # 年化


def _compute_relative_strength(
    stock_bars: list[MarketBar],
    index_bars: list[MarketBar],
    window: int = RS_WINDOW,
) -> float:
    """RS相对强度 [-100, 100]

    计算方法：个股涨幅 vs 基准涨幅的差值，归一化
    """
    if len(stock_bars) < window or len(index_bars) < window:
        return 0.0

    stock_ret = (stock_bars[-1].close - stock_bars[-window].close) / stock_bars[-window].close
    index_ret = (index_bars[-1].close - index_bars[-window].close) / index_bars[-window].close

    diff = stock_ret - index_ret
    # 归一化到 [-100, 100]
    return max(-100, min(100, diff * 100))


def _compute_drawdown(bars: list[MarketBar], window: int = 20) -> float:
    """近 N 日最大回撤"""
    if len(bars) < window:
        return 0.0

    closes = [b.close for b in bars[-window:]]
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _compute_liquidity_score(amount: float, turnover_pct: float) -> float:
    """流动性分 [0, 1]

    基于成交额绝对值和换手率综合判定
    """
    amount_score = min(1.0, amount / 1e9)  # 10亿以上满分
    turnover_score = min(1.0, turnover_pct / 10.0)  # 10%换手以上满分
    return round(amount_score * 0.6 + turnover_score * 0.4, 4)


def _compute_latest_pct(bars: list[MarketBar], n_days: int) -> float:
    """最近 N 日涨跌幅"""
    if len(bars) < n_days + 1:
        return 0.0

    latest = bars[-1].close
    prev = bars[-n_days - 1].close
    if prev <= 0:
        return 0.0
    return (latest - prev) / prev * 100


def _get_mean_volume(bars: list[MarketBar], n_days: int) -> float:
    """N 日均量"""
    if len(bars) < n_days:
        return 0.0
    vols = [b.volume for b in bars[-n_days:]]
    return sum(vols) / len(vols) if vols else 0.0


# ── 主入口 ──


def enrich_stock(
    raw_stock: dict,
    index_bars: Optional[list[MarketBar]] = None,
) -> EnrichedStockSnapshot:
    """
    对一只原始股票数据进行 enrich

    raw_stock: market_filter 输出的单只股票 dict
    index_bars: 可选，基准指数日K，用于 RS 计算
    """
    symbol = raw_stock.get("code", "")
    if not symbol:
        symbol = raw_stock.get("symbol", "")

    enriched = EnrichedStockSnapshot(
        symbol=symbol,
        name=raw_stock.get("name", ""),
        price=raw_stock.get("price", 0.0),
        change_pct=raw_stock.get("change_pct", 0.0),
        volume=raw_stock.get("volume", 0.0),
        amount=raw_stock.get("amount", 0.0),
        turnover_pct=raw_stock.get("turnover", 0.0),
    )

    # ── Step 1: 从 Warehouse 加载日K ──
    bars = _load_cached_bars(symbol, days=65)

    if bars and len(bars) >= 5:
        enriched.has_warehouse_data = True
        enriched.data_source = SOURCE_WAREHOUSE
        enriched.bars_5d = bars[-5:] if len(bars) >= 5 else bars
        enriched.bars_10d = bars[-10:] if len(bars) >= 10 else bars
        enriched.bars_20d = bars[-20:] if len(bars) >= 20 else bars

        # ── 5日 / 10日涨跌幅 ──
        enriched.pct_5d = _compute_latest_pct(bars, 5)
        enriched.pct_10d = _compute_latest_pct(bars, 10)

        # ── 量比 ──
        avg_vol_5 = _get_mean_volume(bars, 5)
        avg_vol_20 = _get_mean_volume(bars, 20)
        enriched.avg_volume_5 = avg_vol_5
        enriched.avg_volume_20 = avg_vol_20
        enriched.volume_ratio = _compute_volume_ratio(raw_stock.get("volume", 0), avg_vol_5, avg_vol_20)

        # ── ATR ──
        enriched.atr, enriched.atr_pct = _compute_atr(bars)

        # ── 波动率 ──
        enriched.volatility = _compute_volatility(bars)

        # ── 最大回撤 ──
        enriched.drawdown_pct = _compute_drawdown(bars)
        enriched.max_high_20d = max(b.close for b in bars[-20:])
        enriched.min_low_20d = min(b.low for b in bars[-20:])

        # ── RS 相对强度 ──
        if index_bars and len(index_bars) >= RS_WINDOW:
            enriched.relative_strength = _compute_relative_strength(bars, index_bars)

        logger.debug(f"[DataEnrich] ✓ {symbol} warehouse-enriched")
    else:
        # ── Step 2: Warehouse 无数据，用实时快照 + 合理 fallback ──
        enriched.data_source = SOURCE_REALTIME

        # 只用当日 snapshot 能提供的信息
        enriched.pct_5d = raw_stock.get("change_pct_5d", 0.0)
        enriched.pct_10d = raw_stock.get("change_pct_10d", 0.0)
        enriched.volume_ratio = raw_stock.get("volume_ratio", 1.0)

        # ATR/波动率用当日涨跌幅估测
        abs_chg = abs(raw_stock.get("change_pct", 0.0))
        enriched.atr_pct = abs_chg if abs_chg > 0 else 2.0
        enriched.atr = enriched.atr_pct / 100 * raw_stock.get("price", 10)
        enriched.volatility = abs_chg * 2  # 粗糙估测

        # 流动性
        enriched.liquidity_score = _compute_liquidity_score(
            raw_stock.get("amount", 0), raw_stock.get("turnover", 0)
        )

        logger.debug(f"[DataEnrich] ~ {symbol} realtime-fallback")

    # ── Step 3: 流动性评分（只要有 amount/turnover 就能算） ──
    enriched.liquidity_score = _compute_liquidity_score(
        enriched.amount, enriched.turnover_pct
    )

    return enriched


def enrich_batch(
    raw_stocks: list[dict],
) -> list[EnrichedStockSnapshot]:
    """
    批量 enrich

    内部会缓存 Warehouse 查询结果，全局只查询一次。
    """
    # 加载基准指数日K（用于 RS 计算）
    index_bars: list[MarketBar] = []
    try:
        wh = get_warehouse()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=75)).strftime("%Y%m%d")
        index_bars = wh.query_daily_bars(RS_REF_INDEX, start_date=start, end_date=end)
        logger.debug(f"[DataEnrich] Loaded {len(index_bars)} index bars for {RS_REF_INDEX}")
    except Exception as e:
        logger.debug(f"[DataEnrich] Index RS unavailable: {e}")

    enriched_list = []
    for stock in raw_stocks:
        try:
            enriched = enrich_stock(stock, index_bars)
            enriched_list.append(enriched)
        except Exception as e:
            logger.warning(f"[DataEnrich] FAILED for {stock.get('code','?')}: {e}")
            # fallback：返回空 enrich
            enriched_list.append(EnrichedStockSnapshot(symbol=stock.get("code", "?")))

    return enriched_list


# ── 日K 预热（供 scheduler 使用） ──


def warmup_daily_k(
    codes: list[str],
    days: int = 65,
    max_concurrent: int = 100,
) -> dict:
    """
    批量预热日K到 Warehouse

    在盘后调用，将全市场活跃股日K写入 Parquet。
    """
    from akshare import stock_zh_a_hist
    from schemas import MarketBar, Timeframe

    wh = get_warehouse()
    now = datetime.now()
    start_date = (now - timedelta(days=days + 10)).strftime("%Y%m%d")
    end_date = now.strftime("%Y%m%d")

    results = {"total": len(codes), "success": 0, "failed": 0, "samples": []}

    for idx, code in enumerate(codes):
        try:
            # 检查是否已缓存
            if wh.has_data(code, now.year):
                # 已经有今年数据
                results["success"] += 1
                continue

            df = stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
                timeout=10,
            )

            if df is None or df.empty:
                results["failed"] += 1
                continue

            bars = []
            for _, row in df.iterrows():
                bars.append(MarketBar(
                    code=code,
                    timeframe=Timeframe.DAY,
                    timestamp=pd.Timestamp(row.get("日期")).to_pydatetime(),
                    open=float(row.get("开盘", 0)),
                    high=float(row.get("最高", 0)),
                    low=float(row.get("最低", 0)),
                    close=float(row.get("收盘", 0)),
                    volume=float(row.get("成交量", 0)),
                    amount=float(row.get("成交额", 0)),
                    change_pct=float(row.get("涨跌幅", 0)),
                    turnover_pct=float(row.get("换手率", 0)),
                ))

            wh.store_daily_bars(bars)
            results["success"] += 1

            if idx < 3:
                results["samples"].append(f"{code}: {len(bars)} bars")

        except Exception as e:
            results["failed"] += 1
            logger.debug(f"[Warmup] {code} failed: {e}")

        if (idx + 1) % 50 == 0:
            logger.info(f"[Warmup] {idx + 1}/{len(codes)} done")

    return results
