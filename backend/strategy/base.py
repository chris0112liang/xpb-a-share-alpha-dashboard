"""
strategy/base.py — Strategy 基类

每个策略是一个"状态机规则"：
  输入：WorldState(regime, rotation_speed, leading_sectors, sector_heatmap...)
  输出：on / off + 置信度 + 解释
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class StrategyAction(str, Enum):
    """策略建议行为"""
    ACTIVATE = "activate"       # 建议开启
    CAUTION = "caution"         # 建议谨慎/轻仓
    DEACTIVATE = "deactivate"  # 建议停用
    NEUTRAL = "neutral"        # 不确定，维持现状


@dataclass
class StrategyDecision:
    """单个策略的判定结果"""
    name: str                           # 策略内部名
    display_name: str                   # 展示名
    action: StrategyAction              # 行为
    confidence: float = 0.0             # 置信度 [0, 1]
    explanation: str = ""               # 为什么这么判定
    priority: int = 50                  # 优先级 (0-100, 大的优先)
    risk_level: float = 0.5             # 策略自身风险 [0, 1]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "action": self.action.value,
            "confidence": round(self.confidence, 2),
            "explanation": self.explanation,
            "priority": self.priority,
            "risk_level": round(self.risk_level, 2),
        }


class Strategy:
    """策略基类——所有具体策略继承此类"""

    def __init__(self, name: str, display_name: str, description: str = ""):
        self.name = name
        self.display_name = display_name
        self.description = description

    def evaluate(self, world_state: dict) -> StrategyDecision:
        """
        核心抽象方法：根据 WorldState 判定策略应激活/停用/中性

        参数 world_state 是从 WorldState 模型序列化的 dict
        包含 all fields: regime, rotation_speed, leading_sectors, hot_sectors, 
                         weak_sectors, sector_heatmap, risk_level, lifecycles 等
        """
        raise NotImplementedError
