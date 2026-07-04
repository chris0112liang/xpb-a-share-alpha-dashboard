"""
strategy/strategies/ — Phase 1-B 内置策略实现

所有策略继承 Strategy 基类。
每个策略只做一件事：根据 WorldState 判定自己该"开"还是"关"。
"""

from __future__ import annotations

from strategy.base import Strategy, StrategyDecision, StrategyAction

# ═══════════════════════════════════════════════
# 1. 趋势突破 Trend Breakout
# ═══════════════════════════════════════════════
#
# 适用条件：
#   regime = bull_trend
#   领涨板块处于 acceleration / main_rise_1
#   rotation_speed < 0.5 (主线清晰，非电风扇)
# ═══════════════════════════════════════════════

class TrendBreakout(Strategy):
    def __init__(self):
        super().__init__(
            name="trend_breakout",
            display_name="趋势突破",
            description="主线清晰时，追入强势板块龙头",
        )

    def evaluate(self, ws: dict) -> StrategyDecision:
        regime = ws.get("regime", "rotational_chop")
        rotation = ws.get("rotation_speed", 0.5)
        heatmap = ws.get("sector_heatmap", {})
        risk = ws.get("risk_level", 0.5)
        phase_dist = heatmap.get("phase_distribution", {})
        leading = ws.get("leading_sectors", [])

        # 牛市趋势——直接激活
        if regime == "bull_trend" and rotation < 0.5 and risk < 0.4:
            act = StrategyAction.ACTIVATE
            conf = 0.85 + (1.0 - rotation * 2) * 0.15
            expl = f"牛市趋势：rotation={rotation:.2f}(<0.5)→主线稳定适合追强"
            return StrategyDecision("trend_breakout", "趋势突破", act, min(conf, 1.0), expl, priority=85, risk_level=0.6)

        # 轮动市但有领涨板块在加速——条件激活
        if regime == "rotational_chop" and rotation < 0.7 and len(leading) >= 2:
            act = StrategyAction.CAUTION
            conf = 0.6
            expl = f"轮动市场line_level=rotation={rotation:.2f}, leading={leading[:2]}→轻仓趋势，等主线确认"
            return StrategyDecision("trend_breakout", "趋势突破", act, conf, expl, priority=60, risk_level=0.6)

        # 高波动/退潮——停用
        if regime in ("bear_trend", "high_volatility") or risk > 0.7:
            act = StrategyAction.DEACTIVATE
            conf = 0.8
            expl = f"风险{risk}/{regime}→趋势策略停用"
            return StrategyDecision("trend_breakout", "趋势突破", act, conf, expl, priority=50, risk_level=0.6)

        return StrategyDecision("trend_breakout", "趋势突破", StrategyAction.NEUTRAL, 0.3, "信号不明确", risk_level=0.6)


# ═══════════════════════════════════════════════
# 2. 板块轮动 Sector Rotation
# ═══════════════════════════════════════════════
#
# 适用条件：
#   rotation_speed > 0.5(高轮动)
#   没有明确领涨主线(leading_sectors 分布均匀)
# ═══════════════════════════════════════════════

class SectorRotation(Strategy):
    def __init__(self):
        super().__init__(
            name="sector_rotation",
            display_name="板块轮动",
            description="高轮动市场，低吸板块分歧、高抛加速板块",
        )

    def evaluate(self, ws: dict) -> StrategyDecision:
        regime = ws.get("regime", "rotational_chop")
        rotation = ws.get("rotation_speed", 0.5)
        leading = ws.get("leading_sectors", [])

        # 高速轮动——核心激活条件
        if rotation > 0.7:
            act = StrategyAction.ACTIVATE
            conf = min(0.95, 0.6 + rotation * 0.5)
            expl = f"高轮动speed={rotation:.2f}(>0.7)→板块电风扇，低吸分歧卖加速"
            return StrategyDecision("sector_rotation", "板块轮动", act, conf, expl, priority=80, risk_level=0.4)

        # 中等轮动——谨慎
        if rotation > 0.5:
            act = StrategyAction.CAUTION
            conf = 0.5
            expl = f"中度轮动speed={rotation:.2f}→可适度做轮动，控制节奏"
            return StrategyDecision("sector_rotation", "板块轮动", act, conf, expl, priority=65, risk_level=0.4)

        # 低轮动——停用
        if rotation < 0.3 and regime != "rotational_chop":
            act = StrategyAction.DEACTIVATE
            conf = 0.7
            expl = f"低轮动speed={rotation:.2f}+{regime}→主线已清晰，不做轮动"
            return StrategyDecision("sector_rotation", "板块轮动", act, conf, expl, priority=40, risk_level=0.4)

        return StrategyDecision("sector_rotation", "板块轮动", StrategyAction.NEUTRAL, 0.3, f"轮动speed={rotation:.2f}中等→观望", risk_level=0.4)


# ═══════════════════════════════════════════════
# 3. 分歧低吸 Dip Stabilization
# ═══════════════════════════════════════════════
#
# 适用条件：
#   领涨板块处于 high_divergence 或 acceleration+turning
#   资金尚未系统性撤退
# ═══════════════════════════════════════════════

class DipStabilization(Strategy):
    def __init__(self):
        super().__init__(
            name="dip_stabilization",
            display_name="分歧低吸",
            description="主线分歧时，博弈第二波启动，低吸而非追涨",
        )

    def evaluate(self, ws: dict) -> StrategyDecision:
        rotation = ws.get("rotation_speed", 0.5)
        heatmap = ws.get("sector_heatmap", {})
        phase_dist = heatmap.get("phase_distribution", {})
        turning = heatmap.get("turning_sectors", [])
        regime = ws.get("regime", "rotational_chop")

        # 拐点预警 + 加速板块存在 → 分歧低吸机会
        n_accel = phase_dist.get("acceleration", 0)
        n_diverg = phase_dist.get("high_divergence", 0)

        if len(turning) > 0 and n_accel > 2 and regime in ("rotational_chop", "bull_trend"):
            act = StrategyAction.ACTIVATE
            conf = 0.65 + min(0.3, len(turning) * 0.1)
            expl = f"分歧低吸→{len(turning)}板块拐点预警，加速转分歧中，可逢低承接"
            return StrategyDecision("dip_stabilization", "分歧低吸", act, min(conf, 0.95), expl, priority=70, risk_level=0.5)

        # 分歧板块多 → 低吸机会正在形成
        if n_diverg > 4 and regime != "bear_trend":
            act = StrategyAction.CAUTION
            expl = f"分歧板块{n_diverg}个→分歧扩散中，等龙头企稳分批低吸"
            return StrategyDecision("dip_stabilization", "分歧低吸", act, 0.55, expl, priority=60, risk_level=0.5)

        # 退潮——分歧变崩
        if regime == "bear_trend":
            act = StrategyAction.DEACTIVATE
            expl = "退潮期→分歧不是机会是风险"
            return StrategyDecision("dip_stabilization", "分歧低吸", act, 0.75, expl, priority=40, risk_level=0.5)

        return StrategyDecision("dip_stabilization", "分歧低吸", StrategyAction.NEUTRAL, 0.3, "分歧信号不足", risk_level=0.5)


# ═══════════════════════════════════════════════
# 4. 超跌反弹 Oversold Reversal
# ═══════════════════════════════════════════════
#
# 适用条件：
#   regime = panic_reversal(恐慌底)
#   冰点修复板块数量 > 3
# ═══════════════════════════════════════════════

class OversoldReversal(Strategy):
    def __init__(self):
        super().__init__(
            name="oversold_reversal",
            display_name="超跌反弹",
            description="恐慌/冰点修复阶段，布局超跌龙头博弈反弹",
        )

    def evaluate(self, ws: dict) -> StrategyDecision:
        regime = ws.get("regime", "rotational_chop")
        risk = ws.get("risk_level", 0.5)
        heatmap = ws.get("sector_heatmap", {})
        phase_dist = heatmap.get("phase_distribution", {})
        n_ice = phase_dist.get("ice_recovery", 0)
        n_decay = phase_dist.get("decay", 0)

        # 恐慌反转——直接激活
        if regime == "panic_reversal":
            act = StrategyAction.ACTIVATE
            conf = 0.8 + n_ice * 0.03
            expl = f"恐慌反转regime+冰点修复{n_ice}个板块→超跌反弹窗口打开"
            return StrategyDecision("oversold_reversal", "超跌反弹", act, min(conf, 0.95), expl, priority=75, risk_level=0.55)

        # 系统性退潮中出现大量冰点修复信号
        if regime == "bear_trend" and n_ice >= 5:
            act = StrategyAction.CAUTION
            expl = f"下跌中出现{n_ice}个冰点修复→可能阶段性底部，轻仓试错"
            return StrategyDecision("oversold_reversal", "超跌反弹", act, 0.5, expl, priority=55, risk_level=0.55)

        # 牛市不做超跌
        if regime == "bull_trend" and n_ice < 3:
            act = StrategyAction.DEACTIVATE
            expl = "牛市不接飞刀，专注趋势"
            return StrategyDecision("oversold_reversal", "超跌反弹", act, 0.7, expl, priority=35, risk_level=0.55)

        return StrategyDecision("oversold_reversal", "超跌反弹", StrategyAction.NEUTRAL, 0.2, "信号不足", risk_level=0.55)


# ═══════════════════════════════════════════════
# 5. 空仓防御 Cash Defense
# ═══════════════════════════════════════════════
#
# 适用条件：
#   regime = bear_trend
#   退潮板块 > 40%
#   多数板块处于 decay
# ═══════════════════════════════════════════════

class CashDefense(Strategy):
    def __init__(self):
        super().__init__(
            name="cash_defense",
            display_name="空仓防御",
            description="系统性风险时，最大仓位建议<20%",
        )

    def evaluate(self, ws: dict) -> StrategyDecision:
        regime = ws.get("regime", "rotational_chop")
        risk = ws.get("risk_level", 0.5)
        heatmap = ws.get("sector_heatmap", {})
        phase_dist = heatmap.get("phase_distribution", {})
        total = heatmap.get("total_sectors", 1) or 1
        n_decay = phase_dist.get("decay", 0)
        decay_ratio = n_decay / total

        # 熊市 or 极度风险
        if regime == "bear_trend" or risk > 0.8:
            act = StrategyAction.ACTIVATE
            conf = 0.85 + decay_ratio * 0.15
            expl = f"熊市{regime}或risk>{risk:.2f}→退潮板块{decay_ratio:.0%}→空仓等待"
            return StrategyDecision("cash_defense", "空仓防御", act, min(conf, 1.0), expl, priority=90, risk_level=0.1)

        # 退潮过半
        if decay_ratio > 0.4 and risk > 0.5:
            act = StrategyAction.ACTIVATE
            expl = f"退潮板块{decay_ratio:.0%}→系统性风险，建议<20%仓位"
            return StrategyDecision("cash_defense", "空仓防御", act, 0.7, expl, priority=85, risk_level=0.1)

        return StrategyDecision("cash_defense", "空仓防御", StrategyAction.NEUTRAL, 0.0, "市场正常", risk_level=0.1)
