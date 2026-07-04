"""
strategy/registry.py — 策略注册中心

所有策略在此注册，selector 通过 registry 获取所有可用策略。

功能：
  1. 策略注册表（dict[name → Strategy]）
  2. 策略分类标签（type/category）
  3. 策略元数据查询
"""

from __future__ import annotations

from strategy.base import Strategy
from strategy.strategies import (
    TrendBreakout,
    SectorRotation,
    DipStabilization,
    OversoldReversal,
    CashDefense,
)

# 策略分类标签
STRATEGY_TAGS = {
    "trend_breakout":      ["trend", "bull", "aggressive"],
    "sector_rotation":     ["rotation", "neutral", "hedging"],
    "dip_stabilization":   ["reversal", "neutral", "opportunity"],
    "oversold_reversal":   ["reversal", "panic", "contrarian"],
    "cash_defense":        ["defense", "risk_off", "conservative"],
}

# 策略注册中心——懒初始化
_registry: dict[str, Strategy] = {}


def get_registry() -> dict[str, Strategy]:
    """获取策略注册表（懒初始化）"""
    global _registry
    if not _registry:
        _register_builtin_strategies()
    return _registry


def get_strategy(name: str) -> Strategy | None:
    """按名称获取策略"""
    return get_registry().get(name)


def list_strategies() -> list[dict]:
    """列出所有策略的元数据"""
    return [
        {
            "name": s.name,
            "display_name": s.display_name,
            "description": s.description,
            "tags": STRATEGY_TAGS.get(s.name, []),
        }
        for s in get_registry().values()
    ]


def _register_builtin_strategies():
    """注册内置策略"""
    _registry["trend_breakout"] = TrendBreakout()
    _registry["sector_rotation"] = SectorRotation()
    _registry["dip_stabilization"] = DipStabilization()
    _registry["oversold_reversal"] = OversoldReversal()
    _registry["cash_defense"] = CashDefense()
