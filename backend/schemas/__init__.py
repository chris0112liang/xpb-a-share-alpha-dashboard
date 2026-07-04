"""统一金融语言层 - Schema 定义

所有模块（DataBus / API / Regime / Agent / Frontend）只认这些 schema。
这是 AI Alpha OS 的金融语义基础。

设计原则：
1. 每个 schema 是自描述的——字段名 + 类型 + docstring 足以推演用途
2. 不混杂业务逻辑——只做数据结构定义
3. Timestamp 统一使用 datetime，前端自行格式化
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════
# 市场行情层
# ═══════════════════════════════════════════

class MarketTicker(str, Enum):
    """统一市场标识"""
    CSI300 = "000300"
    CSI500 = "000905"
    CSI1000 = "000852"
    SSE = "000001"
    SZSE = "399001"
    CHINEXT = "399006"
    STAR50 = "000688"
    BEI50 = "899050"


class Timeframe(str, Enum):
    """多周期枚举——支持 1m/5m/15m/1h/day/week/month"""
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    HOUR_1 = "1h"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class MarketBar(BaseModel):
    """统一 OHLCV 数据条"""
    code: str
    name: str = ""
    timeframe: Timeframe
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0               # 成交额（万元）
    adj_factor: float = 1.0           # 复权因子
    pre_close: float = 0.0
    change_pct: float = 0.0           # 涨跌幅 %
    turnover_pct: float = 0.0         # 换手率 %
    volume_ratio: float = 1.0         # 量比（与 5 日均量比较）


class IndexBar(BaseModel):
    """指数 OHLCV（无涨跌停概念的特殊处理）"""
    code: str
    name: str
    timeframe: Timeframe
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    advance_count: int = 0           # 上涨家数
    decline_count: int = 0           # 下跌家数
    change_pct: float = 0.0


# ═══════════════════════════════════════════
# 因子层
# ═══════════════════════════════════════════

class FactorType(str, Enum):
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    CAPITAL_FLOW = "capital_flow"
    VOLUME_PRICE = "volume_price"
    BREADTH = "breadth"
    LIQUIDITY = "liquidity"
    MONEY_FLOW = "money_flow"
    NORTHBOUND = "northbound"


class FactorSnapshot(BaseModel):
    """某个时间点的因子快照"""
    factor_type: FactorType
    factor_name: str
    value: float
    zscore: float = 0.0           # 相对历史分布的 z-score
    percentile: float = 0.0       # 相对历史分布的百分位 [0,1]
    timestamp: datetime
    code: str = ""                 # 空 = 全市场因子
    metadata: dict[str, Any] = {}


# ═══════════════════════════════════════════
# 市场状态层
# ═══════════════════════════════════════════

class MarketRegime(str, Enum):
    """5 种市场状态——Regime Engine 的输出"""
    BULL_TREND = "bull_trend"              # 牛趋势：上涨趋势明确，宽度好
    BEAR_TREND = "bear_trend"              # 熊趋势：下跌趋势明确，宽度差
    HIGH_VOLATILITY = "high_volatility"    # 高波动：大起大落，无方向
    ROTATIONAL_CHOP = "rotational_chop"    # 轮动震荡：板块快速轮动，无主线
    PANIC_REVERSAL = "panic_reversal"      # 恐慌反转：急跌后 V 反/极端情绪


class DominantStyle(str, Enum):
    """市场风格"""
    LARGE_CAP_VALUE = "large_cap_value"       # 大市值价值（上证50风格）
    LARGE_CAP_GROWTH = "large_cap_growth"     # 大市值成长（创业板权重）
    SMALL_CAP_GROWTH = "small_cap_growth"     # 小市值成长（题材炒作）
    DEFENSIVE = "defensive"                   # 防御（公用事业/红利）
    COMMODITY_CYCLE = "commodity_cycle"       # 周期


class LiquidityState(str, Enum):
    ABUNDANT = "abundant"
    NORMAL = "normal"
    TIGHT = "tight"
    CRISIS = "crisis"


class VolatilityState(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


class SignalGroup(BaseModel):
    """信号层输出——Regime Engine 的输入"""
    breadth_score: float = Field(..., ge=-1, le=1)        # 涨跌家数比
    volatility_score: float = Field(..., ge=0, le=1)      # 波动率水平
    turnover_score: float = Field(..., ge=0, le=1)        # 换手率水平
    liquidity_score: float = Field(..., ge=0, le=1)       # 流动性水平
    momentum_dispersion: float = Field(..., ge=0, le=1)   # 动量分散度
    northbound_flow: float = 0.0                          # 北向资金净流入（归一化）
    sector_concentration: float = Field(..., ge=0, le=1)  # 板块集中度
    top_line_strength: float = Field(..., ge=0, le=1)     # 主线强度


class RegimeState(BaseModel):
    """Regime Engine 的输出——市场是什么状态"""
    regime: MarketRegime
    confidence: float = Field(..., ge=0, le=1)
    dominant_style: DominantStyle
    risk_level: float = Field(..., ge=0, le=1)
    features_used: list[str] = []
    signals: SignalGroup
    state_model_explanation: str = ""      # AI 解释——为什么是这种 regime
    timestamp: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════
# 板块生命周期层
# ═══════════════════════════════════════════

class LifecyclePhase(str, Enum):
    """6 阶段板块生命周期"""
    STARTUP = "startup"                    # 启动
    MAIN_RISE_1 = "main_rise_1"           # 主升一期
    ACCELERATION = "acceleration"          # 加速
    HIGH_DIVERGENCE = "high_divergence"    # 高位分歧
    DECAY = "decay"                        # 退潮
    ICE_RECOVERY = "ice_recovery"          # 冰点修复
    NOISE = "noise"                        # 数据不足（无明确阶段信号）
    DETECT_FAILED = "detect_failed"        # 感知中断（网络/数据源不可用）


class SectorState(BaseModel):
    """板块状态"""
    sector_name: str
    sector_code: str
    lifecycle: LifecyclePhase
    phase_confidence: float = Field(..., ge=0, le=1)
    momentum: float = Field(..., ge=-1, le=1)       # 板块动量 [-1, 1]
    strength_rank: int = 0                           # 在所有板块中的排名
    top_stocks: list[str] = []                       # 板块内 TOP 3 个股
    change_pct_5d: float = 0.0
    change_pct_20d: float = 0.0
    volume_ratio: float = 1.0


class SectorRanking(BaseModel):
    """板块动量排名"""
    rankings: list[SectorState]
    timestamp: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════
# 世界状态——系统大脑
# ═══════════════════════════════════════════

class WorldState(BaseModel):
    """AI Alpha OS 的统一世界状态

    所有子系统最终输出到这里：
    - Regime Engine → regime + risk + style
    - Sector Engine → hot/weak sectors
    - Signals → breadth/momentum scores
    - Factor Engine → factor insights
    - Strategy Engine → active strategies

    前端只有一个端点：GET /api/alpha/world
    策略/Agent 只有一个输入：WorldState
    """
    timestamp: datetime = Field(default_factory=datetime.now)

    # 市场状态
    regime: MarketRegime
    regime_confidence: float = 0.0
    risk_level: float = 0.0
    liquidity_state: LiquidityState = LiquidityState.NORMAL
    volatility_state: VolatilityState = VolatilityState.NORMAL
    dominant_style: DominantStyle = DominantStyle.LARGE_CAP_VALUE

    # 信号得分
    breadth_score: float = 0.0
    momentum_score: float = 0.0
    sector_concentration: float = 0.0
    sentiment_score: float = 0.0

    # 板块
    hot_sectors: list[str] = []        # TOP 3 板块名
    weak_sectors: list[str] = []       # BOTTOM 3 板块名
    leading_sectors: list[str] = []    # 领涨板块（主线方向）
    rotation_speed: float = 0.5        # 轮动速度 [0, 1]（0=无轮动 1=极快轮动）
    sector_heatmap: dict = {}          # 板块热力图（详尽的板块运动数据）
    lifecycles: dict[str, LifecyclePhase] = {}  # 板块名 → 生命周期阶段

    # AI 策略建议
    active_strategies: list[str] = []  # 当前市场适配的策略列表
    strategy_explanation: str = ""     # AI 解释为什么推荐这些策略

    # 元数据
    sources_used: list[str] = []
    computed_at: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════
# Alpha 候选股
# ═══════════════════════════════════════════

from .alpha_candidate import AlphaCandidate, AlphaScanReport
from .alpha_snapshot import AlphaSnapshot, MarketEvent, AiExplanation


# ═══════════════════════════════════════════
# 事件类型（用于 event_bus）
# ═══════════════════════════════════════════

class MarketEventType(str, Enum):
    REGIME_CHANGED = "market.regime_changed"
    LIQUIDITY_COLLAPSED = "market.liquidity_collapsed"
    SECTOR_MOMENTUM_SHIFT = "market.sector_momentum_shifted"
    RISK_OFF_TRIGGERED = "market.risk_off_triggered"
    NORTHBOUND_SURGE = "market.northbound_surge"
    BREADTH_EXTREME = "market.breadth_extreme"
    VOLATILITY_SPIKE = "market.volatility_spike"
    NEW_STRATEGY_ACTIVATED = "system.new_strategy_activated"


class MarketEvent(BaseModel):
    """事件总线的统一事件格式"""
    event_type: MarketEventType
    payload: dict[str, Any]
    severity: str = "info"  # info / warning / critical
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str = ""
