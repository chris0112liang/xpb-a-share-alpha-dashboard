"""Regime Engine —— 市场状态引擎 2.0

架构：
  signals/  (信号层——只算数值)
      ↓
  state_model/  (状态模型——解释市场行为)
      ↓
  engine/  (输出层——Regime + WorldState)

Engine 是最终输出端，组合 signals + state_model 输出：
1. RegimeState（5 种市场状态）
2. WorldState（系统大脑完整状态）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np

from schemas import (
    MarketRegime, RegimeState, SignalGroup, WorldState,
    LifecyclePhase, LiquidityState, VolatilityState, DominantStyle,
)
from databus import DataBus
from regime.signals import MarketSignals
from regime.state_model import StateModel, StateInterpretation

logger = logging.getLogger(__name__)


class RegimeEngine:
    """市场状态引擎

    用法：
        engine = RegimeEngine()
        world = engine.compute_world()
        print(world.regime, world.hot_sectors)
    """

    def __init__(self, bus: Optional[DataBus] = None):
        self._bus = bus or DataBus()
        self._signals = MarketSignals(bus)
        self._model = StateModel()

        # 缓存上一次结果（用于 diff + 事件生成）
        self._last_world: Optional[WorldState] = None

    def compute_regime(self) -> RegimeState:
        """计算当前市场状态 → RegimeState"""
        signals = self._signals.compute_all()
        interpretation = self._model.interpret(signals)

        return RegimeState(
            regime=interpretation.regime,
            confidence=round(interpretation.confidence, 4),
            dominant_style=interpretation.dominant_style,
            risk_level=round(self._compute_risk_level(interpretation), 4),
            features_used=[
                "breadth", "volatility", "turnover", "liquidity",
                "momentum_dispersion", "sector_concentration", "top_line_strength",
            ],
            signals=signals,
            state_model_explanation=interpretation.explanation,
        )

    def compute_world(self) -> WorldState:
        """计算统一世界状态 → WorldState（系统大脑）"""
        try:
            regime = self.compute_regime()
        except Exception as e:
            logger.error(f"Regime compute failed: {e}")
            # 复用已有缓存，避免 DuckDB 锁冲突导致永久 init
            if self._last_world is not None:
                logger.info("compute_world 复用缓存 (上次成功结果)")
                return self._last_world
            # 从未成功过 → 返回安全的默认状态
            regime = RegimeState(
                regime=MarketRegime.ROTATIONAL_CHOP,
                confidence=0.0,
                dominant_style=DominantStyle.LARGE_CAP_VALUE,
                risk_level=0.5,
                signals=SignalGroup(
                    breadth_score=0, volatility_score=0, turnover_score=0,
                    liquidity_score=0, momentum_dispersion=0,
                    sector_concentration=0, top_line_strength=0,
                ),
                state_model_explanation="Regime Engine 未就绪",
            )

        # ── Sector Momentum 集成 ──
        momentum_data = self._get_sector_momentum_data()
        ranking = momentum_data.get("ranking", [])
        rotation_speed = momentum_data.get("rotation_speed", 0.5)
        heatmap = momentum_data.get("heatmap", {})
        leading = momentum_data.get("leading_sectors", [])

        # ── 动态策略选择 ──
        strategy_result = self._compute_strategy_selection(regime, momentum_data)

        # 构建 WorldState
        world = WorldState(
            regime=regime.regime,
            regime_confidence=regime.confidence,
            risk_level=regime.risk_level,
            liquidity_state=self._infer_liquidity(regime.signals.liquidity_score),
            volatility_state=self._infer_volatility(regime.signals.volatility_score),
            dominant_style=regime.dominant_style,
            breadth_score=round(regime.signals.breadth_score, 4),
            momentum_score=round(self._compute_momentum_score(regime), 4),
            sector_concentration=round(regime.signals.sector_concentration, 4),
            sentiment_score=round(self._compute_sentiment(regime), 4),
            hot_sectors=self._get_hot_sectors(ranking=ranking, top_n=3),
            weak_sectors=self._get_weak_sectors(ranking=ranking, top_n=3),
            leading_sectors=leading,
            rotation_speed=rotation_speed,
            sector_heatmap=heatmap,
            lifecycles=self._get_lifecycles(),
            active_strategies=strategy_result.get("active_strategies", self._suggest_strategies(regime)),
            strategy_explanation=strategy_result.get("explanation", self._generate_strategy_explanation(regime)),
            sources_used=["AKShare TX", "Sina", "Sector Lifecycle", "Sector Momentum", "Strategy Selector"],
        )

        self._last_world = world
        return world

    def _compute_risk_level(self, interp: StateInterpretation) -> float:
        """计算风险水平 [0, 1]

        0.0 = 极低风险（牛市），1.0 = 极高风险（熊市/恐慌）
        """
        regime_risk = {
            MarketRegime.BULL_TREND: 0.2,
            MarketRegime.ROTATIONAL_CHOP: 0.5,
            MarketRegime.HIGH_VOLATILITY: 0.6,
            MarketRegime.PANIC_REVERSAL: 0.7,
            MarketRegime.BEAR_TREND: 0.85,
        }
        return regime_risk.get(interp.regime, 0.5)

    def _infer_liquidity(self, lq_score: float) -> LiquidityState:
        if lq_score > 0.7: return LiquidityState.ABUNDANT
        if lq_score > 0.4: return LiquidityState.NORMAL
        if lq_score > 0.2: return LiquidityState.TIGHT
        return LiquidityState.CRISIS

    def _infer_volatility(self, v_score: float) -> VolatilityState:
        if v_score > 0.8: return VolatilityState.EXTREME
        if v_score > 0.6: return VolatilityState.HIGH
        if v_score > 0.3: return VolatilityState.NORMAL
        return VolatilityState.LOW

    def _compute_momentum_score(self, regime: RegimeState) -> float:
        """综合动量分数 [0, 1]"""
        s = regime.signals
        return float(np.clip(
            s.breadth_score * 0.3 + s.liquidity_score * 0.3 +
            s.top_line_strength * 0.2 + (1.0 - s.volatility_score) * 0.2,
            0, 1,
        ))

    def _compute_sentiment(self, regime: RegimeState) -> float:
        """情绪得分 [0, 1]"""
        s = regime.signals
        return float(np.clip(
            s.breadth_score * 0.4 + s.turnover_score * 0.4 +
            s.sector_concentration * 0.2,
            0, 1,
        ))

    def _get_sector_momentum_data(self) -> dict:
        """获取板块动量数据——集中化一次调用"""
        try:
            from sector_worker import SECTOR_LIFECYCLE_FULL
            from sector_momentum import (
                compute_momentum_ranking,
                compute_rotation_speed,
                compute_sector_heatmap,
                get_leading_sectors,
            )

            lifecycles = dict(SECTOR_LIFECYCLE_FULL)
            if not lifecycles:
                # 兜底：板块数据未就绪，用 regime signals 推算轮动速度
                try:
                    sigs = self._signals.compute_all()
                    fallback_rotation = max(0.3, min(0.9, (sigs.breadth_score + sigs.momentum_dispersion) * 0.6))
                except Exception:
                    fallback_rotation = 0.65  # 默认中等偏高轮动
                return {
                    "ranking": [],
                    "rotation_speed": round(fallback_rotation, 2),
                    "heatmap": {},
                    "leading_sectors": [],
                }

            ranking = compute_momentum_ranking(lifecycles)
            rotation_speed = compute_rotation_speed(ranking)
            heatmap = compute_sector_heatmap(lifecycles)
            leading = heatmap.get("leading_sectors", [])

            return {
                "ranking": ranking,
                "rotation_speed": rotation_speed,
                "heatmap": heatmap,
                "leading_sectors": [s["name"] for s in leading],
            }
        except Exception as e:
            logger.error(f"Sector momentum data failed: {e}")
            return {}

    def _compute_strategy_selection(self, regime: RegimeState, momentum_data: dict) -> dict:
        """基于 WorldState 运行策略选择器"""
        try:
            from strategy.selector import strategy_selector

            # 拼装 WorldState dict
            heatmap = momentum_data.get("heatmap", {})
            leading = momentum_data.get("leading_sectors", [])

            world_dict = {
                "regime": regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime),
                "regime_confidence": regime.confidence,
                "risk_level": regime.risk_level,
                "rotation_speed": momentum_data.get("rotation_speed", 0.5),
                "leading_sectors": leading,
                "hot_sectors": self._get_hot_sectors(ranking=momentum_data.get("ranking", []), top_n=3),
                "weak_sectors": self._get_weak_sectors(ranking=momentum_data.get("ranking", []), top_n=3),
                "sector_heatmap": heatmap,
                "lifecycles": {},
                "breadth_score": float(regime.signals.breadth_score),
                "momentum_score": float(regime.signals.momentum_score) if hasattr(regime.signals, "momentum_score") else 0.0,
            }

            result = strategy_selector(world_dict)
            return result
        except Exception as e:
            logger.error(f"Strategy selection failed: {e}")
            return {}

    def _get_hot_sectors(self, ranking: list, top_n: int = 3) -> list[str]:
        """从 momentum ranking 获取热门板块"""
        if not ranking:
            return []
        return [r["name"] for r in ranking[:top_n] if r["momentum_score"] > 0]

    def _get_weak_sectors(self, ranking: list, top_n: int = 3) -> list[str]:
        """从 momentum ranking 获取最弱板块"""
        if not ranking:
            return []
        weak = [r for r in ranking if r.get("phase") in ("decay", "unknown")]
        return [r["name"] for r in weak[:top_n]]

    def _get_lifecycles(self) -> dict[str, LifecyclePhase]:
        """获取所有板块生命周期阶段（兼容旧格式）"""
        try:
            from sector_worker import SECTOR_LIFECYCLE_FULL
            result = {}
            # 6 阶段完整映射（含 detect_failed）
            phase_map = {
                "ice_recovery": LifecyclePhase.ICE_RECOVERY,
                "startup": LifecyclePhase.STARTUP,
                "main_rise_1": LifecyclePhase.MAIN_RISE_1,
                "acceleration": LifecyclePhase.ACCELERATION,
                "high_divergence": LifecyclePhase.HIGH_DIVERGENCE,
                "decay": LifecyclePhase.DECAY,
                "unknown": LifecyclePhase.NOISE,
                "detect_failed": LifecyclePhase.DETECT_FAILED,
            }
            for name, v in SECTOR_LIFECYCLE_FULL.items():
                if isinstance(v, dict):
                    en_phase = v.get("phase", "unknown")
                    result[name] = phase_map.get(en_phase, LifecyclePhase.NOISE)
            return result
        except Exception:
            return {}

    def _suggest_strategies(self, regime: RegimeState) -> list[str]:
        """根据市场状态推荐策略"""
        strategy_map = {
            MarketRegime.BULL_TREND: ["趋势跟踪", "主线龙头", "动量突破"],
            MarketRegime.ROTATIONAL_CHOP: ["波段反转", "低吸高抛", "板块轮动"],
            MarketRegime.HIGH_VOLATILITY: ["日内交易", "期权对冲", "轻仓观望"],
            MarketRegime.PANIC_REVERSAL: ["左侧布局", "超跌反弹", "分批建仓"],
            MarketRegime.BEAR_TREND: ["空仓/现金", "国债/货币", "极严风控"],
        }
        return strategy_map.get(regime.regime, ["观望等待"])

    def _generate_strategy_explanation(self, regime: RegimeState) -> str:
        """生成策略推荐的自然语言解释"""
        descriptions = {
            MarketRegime.BULL_TREND: f"市场处于上升趋势，宽度{regime.signals.breadth_score:.2f}表现良好, 适合积极做多",
            MarketRegime.ROTATIONAL_CHOP: f"板块轮动快速，动量分散{regime.signals.momentum_dispersion:.2f}，适合低吸不追高",
            MarketRegime.HIGH_VOLATILITY: f"波动率{regime.signals.volatility_score:.2f}偏高，适合控制仓位等待方向明朗",
            MarketRegime.PANIC_REVERSAL: f"恐慌情绪可能见底，换手{regime.signals.turnover_score:.2f}极高，关注反转信号",
            MarketRegime.BEAR_TREND: f"下行趋势确认，宽度{regime.signals.breadth_score:.2f}持续恶化，建议降低风险敞口",
        }
        return descriptions.get(regime.regime, "信号不明确，建议观望")


# 全局单例
_engine: Optional[RegimeEngine] = None


def get_regime_engine() -> RegimeEngine:
    global _engine
    if _engine is None:
        _engine = RegimeEngine()
    return _engine


# 兼容 numpy
import numpy as np
