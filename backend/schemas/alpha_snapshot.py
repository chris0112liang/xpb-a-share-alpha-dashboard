"""schemas/alpha_snapshot.py — AlphaSnapshot 统一输出

前端以后只请求 GET /api/alpha/terminal（由一个 AlphaSnapshot 构成）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.alpha_candidate import AlphaCandidate


class MarketEvent(BaseModel):
    """AlphaBrain 生成的预警/事件"""
    event_type: str = ""             # SECTOR_TOPPING / PANIC_REVERSAL / ROTATION_SPIKE / ...
    severity: str = "info"           # info / warning / critical
    confidence: float = Field(default=0.5, ge=0, le=1)
    sector: str = ""
    related_symbols: list[str] = []
    description: str = ""


class AiExplanation(BaseModel):
    """结构化 AI 解释（非技术指标，是市场认知）"""
    regime: str = ""                                          # "轮动震荡"
    rotation_analysis: str = ""                               # "资金高速轮动，板块持续性不足"
    risk_assessment: str = ""                                 # "风险偏好0.39，适中"
    strategy_rationale: str = ""                              # "当前适合低吸分歧，不适合追高加速"
    leading_sector_insight: str = ""                          # "半导体维持主线强度"
    candidate_explanation: str = ""                           # "钢铁板块出现首次加速衰退"
    warning: str = ""
    raw_text: str = ""                                        # 完整段落


class AlphaSnapshot(BaseModel):
    """AlphaBrain.tick() 的统一输出

    前端只请求这一个端点：
      GET /api/alpha/terminal

    包含一切：
    - ts / world_state
    - active_strategies / leading_sectors
    - turning_alerts / top_candidates
    - risk_level / market_bias
    - ai_summary / market_events
    """
    ts: str = ""
    world_state: dict = {}                # WorldState 完整序列化
    regime: str = ""
    regime_display: str = ""
    active_strategies: list[str] = []
    primary_strategy: str = ""
    leading_sectors: list[str] = []
    rotation_speed: float = 0.0
    risk_level: float = 0.5
    market_bias: float = 0.0            # 市场偏向 [-1, 1]（负=空头，正=多头）
    turning_alerts: list[dict] = []     # 拐点预警列表
    top_candidates: list[AlphaCandidate] = []
    market_events: list[MarketEvent] = []
    ai_summary: str = ""
    ai_explanation: AiExplanation = Field(default_factory=AiExplanation)
    computed_at: str = ""
