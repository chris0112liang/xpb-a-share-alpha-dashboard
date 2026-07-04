"""
alpha/ranking.py — 候选股二次排序 & 最终输出组装

职责：
  1. 候选股降分微调（一致性排名、板块龙头溢价、成交活跃度调整）
  2. 组装 AlphaScanReport
"""

from __future__ import annotations

from datetime import datetime

from schemas.alpha_candidate import AlphaCandidate, AlphaScanReport


def build_report(
    world_state: dict,
    strategy_result: dict,
    candidates: list[AlphaCandidate],
) -> AlphaScanReport:
    """
    组装最终扫描报告，含二次排序调整和全局解释
    """
    if candidates:
        candidates = _secondary_ranking(candidates, world_state)

    # 生成全局解释
    explanation = _build_global_explanation(candidates, strategy_result, world_state)

    return AlphaScanReport(
        world_regime=world_state.get("regime", ""),
        rotation_speed=world_state.get("rotation_speed", 0.0),
        active_strategies=strategy_result.get("active_strategies", []),
        primary_strategy=strategy_result.get("primary_strategy", ""),
        total_candidates=len(candidates),
        candidates=candidates,
        scan_time=datetime.now().strftime("%H:%M:%S"),
        explanation=explanation,
    )


def _secondary_ranking(
    candidates: list[AlphaCandidate], world_state: dict
) -> list[AlphaCandidate]:
    """
    二次排序调整：

    1. 龙头溢价：板块动量排名靠前的候选股加分
    2. 成交活跃溢价：成交额排名靠前的候选股加分
    3. 拐点识别：首次回踩 + 缩量的候选股加分
    4. 去极值：分数封顶 100
    """
    adjusted = []
    for c in candidates:
        bonus = 0.0

        # 板块动量排名溢价（前3名板块内的候选股+5分）
        if c.sector_momentum_rank < 3:
            bonus += 5

        # 板块动量排名前1名再+3
        if c.sector_momentum_rank == 0:
            bonus += 3

        # 首次回踩+缩量（最佳低吸形态） +8
        if c.is_first_pullback and c.is_volume_shrink:
            bonus += 8

        # 成交活跃（TOP 200）
        if c.amount_rank < 200:
            bonus += 3

        # 强于板块且强于大盘
        if c.stronger_than_sector and c.stronger_than_index:
            bonus += 5

        # 板块冰点修复溢价（困境反转预期）
        if c.sector_phase == "ice_recovery":
            bonus += 3

        # 风险折扣
        risk_discount = c.risk_level * 5  # risk 0~1 → discount 0~5
        bonus -= risk_discount

        c.score = max(0, min(100, c.score + bonus))
        adjusted.append(c)

    adjusted.sort(key=lambda c: c.score, reverse=True)
    return adjusted


def _build_global_explanation(
    candidates: list[AlphaCandidate],
    strategy_result: dict,
    world_state: dict,
) -> str:
    """生成扫描自然语言总结"""
    regime = world_state.get("regime", "?")
    primary = strategy_result.get("primary_display", "无")
    active = strategy_result.get("active_strategies", [])

    parts = [f"市场{regime}态，主策略：{primary}"]

    if not candidates:
        parts.append("当前无符合条件的候选股。")
        return " | ".join(parts)

    # 按策略分组
    by_strategy: dict[str, list[AlphaCandidate]] = {}
    for c in candidates:
        by_strategy.setdefault(c.triggered_strategy_display, []).append(c)

    for strategy_name, group in sorted(by_strategy.items()):
        top = group[0]
        parts.append(
            f"【{strategy_name}】{len(group)}只候选 "
            f"最强={top.name}({top.symbol}) 评分={top.score:.0f} "
            f"板块阶段={top.sector_phase_cn}"
        )

    # 风险提示
    risk = world_state.get("risk_level", 0.5)
    if risk > 0.6:
        parts.append(f"⚠️ 风险等级{risk:.2f}，注意仓位控制")

    return " | ".join(parts)
