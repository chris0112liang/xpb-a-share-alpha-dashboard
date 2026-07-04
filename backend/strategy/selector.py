"""
strategy/selector.py — 策略动态选择器

核心入口：strategy_selector(world_state) → StrategySelectorOutput

逻辑：
  1. 遍历所有注册策略，每个 strategy.evaluate(world_state)
  2. 收集所有 decision
  3. 构建输出：激活列表 + 停用列表 + 整体解释

输出格式：
{
  "active_strategies": [strategy_name, ...],
  "disabled_strategies": [strategy_name, ...],
  "decisions": [decision_dict, ...],
  "primary_strategy": str,        # 最高优先级激活策略
  "risk_appetite": float,         # 整体风险偏好 [0, 1]
  "explanation": str,             # 自然语言解释
}
"""

from __future__ import annotations

from strategy.base import StrategyAction, StrategyDecision
from strategy.registry import get_registry


def strategy_selector(world_state: dict) -> dict:
    """
    核心函数：WorldState → 策略激活/停用列表

    参数 world_state: WorldState 模型的 dict 序列化
    返回: 策略选择输出
    """
    registry = get_registry()
    if not registry:
        return _empty_result("无可用策略")

    decisions: list[StrategyDecision] = []

    for name, strategy in registry.items():
        try:
            decision = strategy.evaluate(world_state)
            decisions.append(decision)
        except Exception as e:
            # 策略评估失败——中性处理
            decisions.append(StrategyDecision(
                name=name,
                display_name=strategy.display_name,
                action=StrategyAction.NEUTRAL,
                confidence=0.0,
                explanation=f"评估异常: {e}",
            ))

    # 分类
    active_decisions = [d for d in decisions if d.action == StrategyAction.ACTIVATE]
    caution_decisions = [d for d in decisions if d.action == StrategyAction.CAUTION]
    inactive_decisions = [d for d in decisions if d.action == StrategyAction.DEACTIVATE]
    neutral_decisions = [d for d in decisions if d.action == StrategyAction.NEUTRAL]

    # 按优先级排序
    active_decisions.sort(key=lambda d: -d.priority)
    caution_decisions.sort(key=lambda d: -d.priority)

    # 主策略=最高优先级激活
    primary = active_decisions[0] if active_decisions else None

    # 整体风险偏好
    risk_appetite = _compute_risk_appetite(decisions, world_state)

    # 自然语言总结
    explanation = _generate_explanation(active_decisions, caution_decisions, inactive_decisions, risk_appetite, world_state)

    return {
        "active_strategies": [d.name for d in active_decisions],
        "disabled_strategies": [d.name for d in inactive_decisions],
        "decisions": [d.to_dict() for d in decisions],
        "primary_strategy": primary.name if primary else "无",
        "primary_display": primary.display_name if primary else "观望等待",
        "risk_appetite": round(risk_appetite, 2),
        "explanation": explanation,
    }


def _compute_risk_appetite(decisions: list[StrategyDecision], ws: dict) -> float:
    """综合风险偏好 [0, 1] — 0=安全, 1=激进"""
    # 基础：WorldState risk_level
    base_risk = ws.get("risk_level", 0.5)

    # 调节：激活/停用策略的杠杆
    active_risk = sum(d.risk_level for d in decisions if d.action == StrategyAction.ACTIVATE) / max(len([d for d in decisions if d.action == StrategyAction.ACTIVATE]), 1)
    deact_count = sum(1 for d in decisions if d.action == StrategyAction.DEACTIVATE)
    deact_penalty = deact_count * 0.05

    blended = base_risk * 0.5 + active_risk * 0.3 - deact_penalty * 0.2
    return max(0.0, min(1.0, blended))


def _generate_explanation(
    active: list[StrategyDecision],
    caution: list[StrategyDecision],
    inactive: list[StrategyDecision],
    risk_appetite: float,
    ws: dict,
) -> str:
    """生成自然语言策略解释"""
    regime = ws.get("regime", "?")
    rotation = ws.get("rotation_speed", 0.5)
    leading = ws.get("leading_sectors", [])

    parts = []
    parts.append(f"市场处于{regime}态，轮动速度{rotation:.2f}")

    if active:
        pri = active[0]
        detail = "; ".join(f"[{d.display_name}]({d.explanation})" for d in active)
        parts.append(f"主策略激活：{pri.display_name}")
        parts.append(f"详情：{detail}")
    else:
        parts.append("无激活策略——等待信号")

    if caution:
        parts.append("谨慎策略：")
        for d in caution:
            parts.append(f"  {d.display_name}←{d.explanation}")

    if inactive:
        parts.append("已停用策略(原因)：")
        for d in inactive:
            parts.append(f"  {d.display_name}→{d.explanation}")

    parts.append(f"综合风险偏好：{risk_appetite:.2f}")
    return " | ".join(parts)


def _empty_result(reason: str) -> dict:
    return {
        "active_strategies": [],
        "disabled_strategies": [],
        "decisions": [],
        "primary_strategy": "无",
        "primary_display": "无可用策略",
        "risk_appetite": 0.5,
        "explanation": reason,
    }
