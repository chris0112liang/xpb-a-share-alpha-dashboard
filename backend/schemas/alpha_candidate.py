"""
schemas/alpha_candidate.py — Alpha 候选股 Schema

AlphaCandidate 是 Phase 2 的统一输出格式。
无论什么策略产生的候选股，最终输出都是这个结构。
"""

from pydantic import BaseModel, Field
from typing import Optional


class AlphaCandidate(BaseModel):
    """Alpha 扫描候选股

    统一候选股格式：
    - symbol / name / sector → 基本信息
    - score / confidence → 排序依据
    - triggered_strategy → 由哪个策略激活
    - sector_phase → 所属板块生命周期阶段
    - momentum_rank → 板块内动量排名
    - risk_level → 个股风险评级
    - reasons → 结构化解释（多条）
    """
    symbol: str
    name: str
    sector: str = ""

    # 评分
    score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)

    # 策略归属
    triggered_strategy: str = ""
    triggered_strategy_display: str = ""

    # 板块状态
    sector_phase: str = ""
    sector_phase_cn: str = ""
    sector_momentum_rank: int = 0

    # 技术指标（各 scanner 共享）
    change_pct: float = 0.0          # 当日涨跌幅 %
    change_pct_5d: float = 0.0       # 5日涨跌幅 %
    volume_ratio: float = 1.0        # 量比
    turnover_rate: float = 0.0       # 换手率 %
    amount_rank: int = 0             # 成交额排名（全A）

    # 强弱指标
    stronger_than_sector: bool = False   # 强于板块
    stronger_than_index: bool = False    # 强于指数
    relative_strength: float = 0.0       # RS相对强度 [-100, 100]

    # 拐点信号
    is_first_pullback: bool = False      # 首次分歧回踩
    is_volume_shrink: bool = False       # 缩量企稳
    is_volume_expand: bool = False       # 放量突破
    is_breakout: bool = False            # 突破形态

    # 风控
    risk_level: float = Field(default=0.5, ge=0, le=1)

    # Ranking V2 新增
    tier: str = "Watchlist"           # S / A / B / Watchlist
    risk_reward: float = 0.0           # 风险收益比

    # Data Enrichment 字段（真实个股数据增强）
    atr: float = 0.0                  # 14日 ATR（价格单位）
    atr_pct: float = 0.0              # ATR 百分比
    volatility: float = 0.0           # 20日年化波动率
    drawdown_pct: float = 0.0         # 近20日最大回撤 %
    data_source: str = "fallback"    # warehouse / realtime / fallback
    liquidity_score: float = 0.0      # 流动性评分 [0,1]

    # 解释系统（最重要）
    reasons: list[str] = []

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "score": round(self.score, 1),
            "confidence": round(self.confidence, 2),
            "triggered_strategy": self.triggered_strategy,
            "triggered_strategy_display": self.triggered_strategy_display,
            "sector_phase": self.sector_phase,
            "sector_phase_cn": self.sector_phase_cn,
            "sector_momentum_rank": self.sector_momentum_rank,
            "change_pct": round(self.change_pct, 2),
            "change_pct_5d": round(self.change_pct_5d, 2),
            "volume_ratio": round(self.volume_ratio, 2),
            "turnover_rate": round(self.turnover_rate, 2),
            "amount_rank": self.amount_rank,
            "stronger_than_sector": self.stronger_than_sector,
            "stronger_than_index": self.stronger_than_index,
            "relative_strength": round(self.relative_strength, 1),
            "is_first_pullback": self.is_first_pullback,
            "is_volume_shrink": self.is_volume_shrink,
            "is_volume_expand": self.is_volume_expand,
            "is_breakout": self.is_breakout,
            "risk_level": round(self.risk_level, 2),
            "tier": self.tier,
            "risk_reward": round(self.risk_reward, 1),
            "atr": round(self.atr, 4),
            "atr_pct": round(self.atr_pct, 4),
            "volatility": round(self.volatility, 4),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "data_source": self.data_source,
            "liquidity_score": round(self.liquidity_score, 2),
            "reasons": self.reasons,
            "change_pct_10d": round(self.change_pct_10d, 2) if hasattr(self, 'change_pct_10d') else 0.0,
            "pct_10d": round(self.change_pct_10d, 2) if hasattr(self, 'change_pct_10d') else 0.0,
        }


class AlphaScanReport(BaseModel):
    """Alpha 扫描报告

    GET /api/alpha/candidates 的返回结构
    """
    world_regime: str = ""
    rotation_speed: float = 0.0
    active_strategies: list[str] = []
    primary_strategy: str = ""
    total_candidates: int = 0
    candidates: list[AlphaCandidate] = []
    scan_time: str = ""
    explanation: str = ""
