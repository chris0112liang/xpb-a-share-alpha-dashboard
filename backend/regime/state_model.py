"""State Model 层——解释市场行为

Signals 层给出原始信号值。
State Model 层解释这些信号组合意味着什么。

设计原则：
- 不是指标打分（if score > 70: bull）
- 而是模式识别：当前信号组合匹配哪种市场状态模式
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from schemas import MarketRegime, DominantStyle, SignalGroup

logger = logging.getLogger(__name__)


@dataclass
class StateInterpretation:
    """状态模型解释输出"""
    regime: MarketRegime
    confidence: float
    dominant_style: DominantStyle
    explanation: str


class StateModel:
    """市场状态模型——解释器

    输入：SignalGroup
    输出：StateInterpretation
    """

    def interpret(self, signals: SignalGroup) -> StateInterpretation:
        """将信号组解释为市场状态

        决策树——不是打分。每个路径基于市场行为模式。
        """
        b = signals.breadth_score
        v = signals.volatility_score
        t = signals.turnover_score
        lq = signals.liquidity_score
        md = signals.momentum_dispersion
        sc = signals.sector_concentration
        tl = signals.top_line_strength

        # ── 模式 1: 熊趋势 ──
        # 宽度极差 + 流动性下降 + 低换手
        if b < -0.5 and lq < 0.35:
            return StateInterpretation(
                regime=MarketRegime.BEAR_TREND,
                confidence=min(1.0, abs(b) + (1.0 - lq) * 0.5),
                dominant_style=DominantStyle.DEFENSIVE,
                explanation=f"宽度{b:.2f}显示普跌，流动性{lq:.2f}收缩，市场处于下行趋势",
            )

        # ── 模式 2: 高波动 ──
        # 波动率高 + 换手率高 + 宽度方向不定
        if v > 0.7 and t > 0.6 and abs(b) < 0.4:
            return StateInterpretation(
                regime=MarketRegime.HIGH_VOLATILITY,
                confidence=min(1.0, v * 0.5 + t * 0.5),
                dominant_style=DominantStyle.SMALL_CAP_GROWTH,
                explanation=f"波动{v:.2f}偏高，换手{t:.2f}活跃，宽幅震荡中",
            )

        # ── 模式 3: 牛市趋势 ──
        # 宽度好 + 流动性好 + 主线明确 + 低波动
        if b > 0.3 and lq > 0.5 and sc > 0.6 and v < 0.6:
            return StateInterpretation(
                regime=MarketRegime.BULL_TREND,
                confidence=min(1.0, b * 0.3 + lq * 0.3 + sc * 0.4),
                dominant_style=self._infer_style(signals),
                explanation=f"宽度{b:.2f}向好，流动性{lq:.2f}充足，板块集中度{sc:.2f}主线明确",
            )

        # ── 模式 4: 轮动震荡 ──
        # 宽度中性 + 动量分散 + 板块集中度低
        if abs(b) < 0.3 and md > 0.4 and sc < 0.5:
            return StateInterpretation(
                regime=MarketRegime.ROTATIONAL_CHOP,
                confidence=min(1.0, (1.0 - abs(b)) * 0.3 + md * 0.4 + (1.0 - sc) * 0.3),
                dominant_style=self._infer_style(signals),
                explanation=f"动量分散{md:.2f}板块无明确主线，各板块快速轮动",
            )

        # ── 模式 5: 恐慌反转 ──
        # 宽度极差 + 波动极高 + 高换手（V反特征）
        if b < -0.3 and v > 0.6 and t > 0.7:
            return StateInterpretation(
                regime=MarketRegime.PANIC_REVERSAL,
                confidence=min(1.0, abs(b) * 0.3 + v * 0.3 + t * 0.4),
                dominant_style=self._infer_style(signals),
                explanation=f"宽度{b:.2f}差但换手{t:.2f}极高，恐慌放量可能为反转信号",
            )

        # ── 兜底: 震荡 ──
        # 所有模式都不匹配
        return StateInterpretation(
            regime=MarketRegime.ROTATIONAL_CHOP,
            confidence=0.4,
            dominant_style=self._infer_style(signals),
            explanation=f"信号未明确指向特定模式(b={b:.2f} v={v:.2f} lq={lq:.2f}), 归类为轮动震荡",
        )

    def _infer_style(self, signals: SignalGroup) -> DominantStyle:
        """推断主导风格（简化版）"""
        b = signals.breadth_score
        tl = signals.top_line_strength

        if tl > 0.7 and b > 0.3:
            # 强主线 + 好宽度 → 大市值
            return DominantStyle.LARGE_CAP_GROWTH
        elif b > 0.3 and tl < 0.4:
            # 宽度好但无主线 → 小票普涨
            return DominantStyle.SMALL_CAP_GROWTH
        elif b < -0.3:
            # 宽度差 → 防御
            return DominantStyle.DEFENSIVE
        else:
            return DominantStyle.LARGE_CAP_VALUE
