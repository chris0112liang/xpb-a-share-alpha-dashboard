"""alpha/ranking_v2.py — Candidate Ranking 2.0

AlphaScore = StrategyFit * SectorStrength * RelativeStrength * VolumeQuality
             * MomentumQuality * RiskReward * LiquidityScore
           - ExhaustionPenalty - CrowdedPenalty

不同策略使用不同的 ranking 逻辑。
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ── Constantes ──
# 每项成分的满分
MAX_FIT = 1.0
MAX_STRENGTH = 1.0
MAX_RELATIVE = 1.0
MAX_VOLUME = 1.0
MAX_MOMENTUM = 1.0
MAX_RISK_REWARD = 1.0
MAX_LIQUIDITY = 1.0
MAX_PENALTY = 0.3  # 每项罚分上限

BASE_SCORE = 50.0       # 基准分
SCORE_RANGE = 50.0      # 上下浮动 ±50


def compute_score(stock: dict, strategy_name: str, world: dict, sector_info: dict | None) -> dict:
    """主入口：根据策略计算个股最终评分

    返回：
      {
        "score": float,
        "confidence": float,
        "risk_reward": float,
        "crowdedness": float,
        "components": { ... },
        "reasons": [str, ...],
        "tier": "S" | "A" | "B" | "Watchlist",
      }
    """
    # ── 提取基础指标（优先使用 EnrichedStockSnapshot 的真实数据） ──
    change_pct = stock.get("change_pct", 0.0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    amount = stock.get("amount", 1.0)  # 成交额（元）

    # data_enrichment 注入的真实数据
    pct_5d = stock.get("pct_5d", 0.0)
    pct_10d = stock.get("pct_10d", 0.0)
    atr_pct = stock.get("atr_pct", 0.0)
    volatility = stock.get("volatility", 0.0)
    relative_strength = stock.get("relative_strength", 0.0)
    drawdown_pct = stock.get("drawdown_pct", 0.0)
    turnover_pct = stock.get("turnover", 0.0) or stock.get("turnover_pct", 0.0)
    data_source = stock.get("data_source", "fallback")

    sector_phase = (sector_info or {}).get("phase", "noise") if sector_info else "noise"
    is_leading = (sector_info or {}).get("is_leading", False) if sector_info else False

    # ── 分策略计算各项得分 ──
    strategy_key = strategy_name

    extra = {
        "pct_5d": pct_5d,
        "pct_10d": pct_10d,
        "atr_pct": atr_pct,
        "volatility": volatility,
        "relative_strength": relative_strength,
        "drawdown_pct": drawdown_pct,
        "turnover_pct": turnover_pct,
        "data_source": data_source,
    }

    components = _compute_components(
        strategy_key, change_pct, volume_ratio, amount, sector_phase, is_leading, world, stock, extra
    )

    # ── 综合得分 ──
    raw_score = (
        components["strategy_fit"] *
        components["sector_strength"] *
        components["relative_strength"] *
        components["volume_quality"] *
        components["momentum_quality"] *
        components["risk_reward"] *
        components["liquidity_score"]
    )

    penalty = components.get("exhaustion_penalty", 0.0) + components.get("crowded_penalty", 0.0)
    raw_score = raw_score - penalty

    # 映射到 0-100
    mapped = BASE_SCORE + raw_score * SCORE_RANGE
    score = max(0.0, min(100.0, mapped))

    # ── 置信度 ──
    confidence = round(components.get("confidence", 0.5), 2)

    # ── 风险收益比 ──
    risk_reward = round(components.get("risk_reward_ratio", 1.0), 1)

    # ── 拥挤度 ──
    crowdedness = round(components.get("crowdedness", 0.5), 2)

    # ── 分级 ──
    tier = _classify(score, confidence, risk_reward, crowdedness)

    # ── 理由 ──
    reasons = _build_reasons(strategy_key, components, sector_phase, change_pct, volume_ratio, risk_reward)

    return {
        "score": round(score, 1),
        "confidence": confidence,
        "risk_reward": risk_reward,
        "crowdedness": crowdedness,
        "tier": tier,
        "components": components,
        "reasons": reasons,
    }


def _compute_components(
    strategy: str, change_pct: float, vol_ratio: float, amount: float,
    sector_phase: str, is_leading: bool, world: dict, stock: dict,
    extra: dict | None = None,
) -> dict:
    """逐项计算各维度得分——策略感知，使用 extra 中的增强数据"""

    if extra is None:
        extra = {}

    if strategy in ("sector_rotation", "dip_stabilization"):
        return _compute_sector_rotation_components(
            change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra
        )
    elif strategy in ("trend_breakout",):
        return _compute_trend_breakout_components(
            change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra
        )
    elif strategy in ("oversold_reversal", "cash_defense", "defensive_cash"):
        return _compute_oversold_reversal_components(
            change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra
        )
    else:
        return _compute_generic_components(
            change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra
        )


# ═══════════════════════════════════════════
# 分策略计算
# ═══════════════════════════════════════════

def _compute_sector_rotation_components(
    change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra,
) -> dict:
    """板块轮动策略评分：

    V2 核心改进：使用真实 RS/ATR/波动率/5日涨跌
    """
    rotation = world.get("rotation_speed", 0.5)
    rs = extra.get("relative_strength", 0.0)
    pct_5d = extra.get("pct_5d", 0.0)
    atr_pct = extra.get("atr_pct", 0.0)
    volatility = extra.get("volatility", 0.0)
    drawdown = extra.get("drawdown_pct", 0.0)
    data_source = extra.get("data_source", "fallback")
    turnover_pct = extra.get("turnover_pct", 0.0)

    has_real_data = data_source in ("warehouse", "realtime")

    # ── StrategyFit：轮动速度匹配度 ──
    fit = 0.6 if rotation > 0.6 else (0.4 if rotation > 0.4 else 0.3)
    if rotation > 0.7:
        fit = 0.8

    # ── SectorStrength ──
    if not has_real_data and data_source in ("cache", "fallback"):
        # 缓存/回退模式下：所有非noise板块都给予合理权重
        # 因为没有实时数据，板块阶段是唯一可用的判断维度
        if sector_phase in ("high_divergence", "main_rise_1"):
            strength = 0.8
        elif sector_phase in ("startup", "acceleration"):
            strength = 0.7
        elif sector_phase in ("ice_recovery",):
            strength = 0.6
        else:
            strength = 0.5  # decay / unknown 也不低于0.5
    elif sector_phase in ("high_divergence",):
        strength = 0.8
    elif sector_phase in ("main_rise_1",):
        strength = 0.7 if is_leading else 0.5
    elif sector_phase in ("startup",):
        strength = 0.6
    elif sector_phase in ("acceleration",):
        strength = 0.3
    else:
        strength = 0.2

    # ── RelativeStrength：使用真实 RS ──
    if has_real_data and rs != 0:
        # RS [-100,100] → [0,1]
        rel = max(0.0, min(1.0, (rs + 100) / 200))
        # 更严格：高 RS 才好
        if rs > 30:
            rel = 0.85
        elif rs > 10:
            rel = 0.7
        elif rs > -10:
            rel = 0.5
        elif rs > -30:
            rel = 0.35
        else:
            rel = 0.2
    else:
        # fallback：区间涨跌
        if -5 < change_pct < 0:
            rel = 0.8
        elif 0 <= change_pct < 3:
            rel = 0.7
        elif change_pct >= 3:
            rel = 0.5
        else:
            rel = 0.3

    # ── VolumeQuality：使用真实量比 + 5日趋势 ──
    if 0.8 <= vol_ratio <= 1.5:
        vol_q = 0.8
    elif vol_ratio < 0.8:
        vol_q = 0.6
    else:
        vol_q = 0.4

    # 5日趋势修正
    if has_real_data:
        if -5 <= pct_5d <= 2:
            vol_q = min(0.9, vol_q + 0.1)  # 5日企稳加置信
        elif pct_5d > 10:
            vol_q = max(0.2, vol_q - 0.15)  # 5日涨幅过大减分

    # ── MomentumQuality：使用真实波动率 + ATR ──
    mom_q = 0.6
    if has_real_data and atr_pct > 0:
        # ATR小 = 企稳，ATR大 = 波动剧烈
        if atr_pct < 2:
            mom_q = 0.75
        elif atr_pct < 4:
            mom_q = 0.6
        else:
            mom_q = 0.4

    if -3 <= change_pct <= 0:
        mom_q = max(mom_q, 0.65)

    # ── RiskReward：使用真实 ATR 估算 ──
    if has_real_data and atr_pct > 0 and volatility > 0:
        # 上行空间 = ATR * 3（合理目标）
        upside_pct = atr_pct * 3
        # 下行风险 = ATR * 1.5 或 drawdown
        downside_pct = max(atr_pct * 1.5, drawdown if drawdown > 0 else atr_pct)
    else:
        upside_pct = stock.get("upside_pct", 5.0)
        downside_pct = max(abs(stock.get("downside_pct", -3.0)), 1.0)

    rr = upside_pct / downside_pct if downside_pct > 0 else 1.0
    rr_score = min(1.0, rr / 3.0)

    # ── LiquidityScore：真实换手率 ──
    if amount > 0:
        liq = min(1.0, amount / 100000000.0)  # 1亿
        if turnover_pct > 0:
            liq = min(1.0, liq * 0.6 + (turnover_pct / 10.0) * 0.4)
    else:
        # fallback 模式下，能进入扫描的股票来自精选池（蓝筹/板块成分股）
        # 流动性天然有保障，不使用0.3的低分
        liq = 0.7 if data_source in ("fallback",) else 0.3

    # ── ExhaustionPenalty ──
    exhaustion = 0.15 if change_pct > 5 else 0.0
    # 5日涨跌幅过大也加惩罚
    if has_real_data and pct_5d > 15:
        exhaustion += 0.1

    # ── CrowdedPenalty ──
    crowdedness = min(1.0, rotation * 0.6)
    if is_leading and rotation > 0.6:
        crowdedness = min(1.0, crowdedness + 0.2)  # 领涨板块更拥挤
    crowded_penalty = crowdedness * 0.15

    # ── Confidence ──
    confidence = 0.5 + 0.3 * (fit + strength) / 2
    if has_real_data:
        confidence = min(0.95, confidence + 0.1)  # 有真实数据加置信

    return {
        "strategy_fit": fit,
        "sector_strength": strength,
        "relative_strength": rel,
        "volume_quality": vol_q,
        "momentum_quality": mom_q,
        "risk_reward": rr_score,
        "risk_reward_ratio": round(rr, 1),
        "liquidity_score": liq,
        "exhaustion_penalty": exhaustion,
        "crowded_penalty": crowded_penalty,
        "crowdedness": crowdedness,
        "confidence": round(confidence, 2),
    }


def _compute_trend_breakout_components(
    change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra,
) -> dict:
    """趋势突破策略评分：

    V2 使用真实 RS/ATR/5日涨跌判断突破真实性
    """
    rotation = world.get("rotation_speed", 0.5)
    rs = extra.get("relative_strength", 0.0)
    pct_5d = extra.get("pct_5d", 0.0)
    atr_pct = extra.get("atr_pct", 0.0)
    volatility = extra.get("volatility", 0.0)
    data_source = extra.get("data_source", "fallback")
    turnover_pct = extra.get("turnover_pct", 0.0)
    has_real_data = data_source in ("warehouse", "realtime")

    # StrategyFit
    fit = 0.8 if rotation < 0.4 else (0.5 if rotation < 0.6 else 0.2)

    # SectorStrength
    if not has_real_data and data_source in ("cache", "fallback"):
        # 缓存/回退模式：所有板块都给予合理权重
        if sector_phase in ("main_rise_1", "acceleration", "startup", "high_divergence"):
            strength = 0.8
        elif sector_phase in ("ice_recovery",):
            strength = 0.7
        else:
            strength = 0.5
    elif sector_phase in ("main_rise_1", "acceleration"):
        strength = 0.9 if is_leading else 0.7
    elif sector_phase in ("startup",):
        strength = 0.5
    else:
        strength = 0.2

    # RelativeStrength：突破 + 真实 RS
    if has_real_data:
        # 真实 RS 高 + 当日涨幅适中 = 健康突破
        if rs > 20 and 2 <= change_pct <= 7:
            rel = 0.9
        elif rs > 10 and change_pct > 0:
            rel = 0.75
        elif rs > -10 and change_pct > 2:
            rel = 0.6
        else:
            rel = 0.3
    else:
        if 2 <= change_pct <= 7:
            rel = 0.9
        elif 0 <= change_pct < 2:
            rel = 0.6
        else:
            rel = 0.3

    # VolumeQuality：放量突破 + 5日趋势确认
    if vol_ratio > 1.5:
        vol_q = 0.9
    elif vol_ratio > 1.2:
        vol_q = 0.7
    elif vol_ratio > 0.8:
        vol_q = 0.5
    else:
        vol_q = 0.3

    if has_real_data:
        if 5 <= pct_5d <= 15:
            vol_q = min(0.95, vol_q + 0.1)  # 5日持续放量
        elif pct_5d < -5:
            vol_q = max(0.2, vol_q - 0.2)  # 5日下跌中的放量可能是抛售

    # MomentumQuality
    mom_q = 0.5
    if has_real_data and volatility > 0:
        if volatility < 0.3:
            mom_q = 0.7  # 低波动率预示持续
        elif volatility < 0.6:
            mom_q = 0.6
        else:
            mom_q = 0.4  # 高波动率易反转

    if 0 < change_pct <= 5:
        mom_q = max(mom_q, 0.75)

    # RiskReward：ATR 估值
    if has_real_data and atr_pct > 0:
        upside_pct = max(atr_pct * 4, 5.0)
        downside_pct = max(atr_pct * 2, atr_pct)
    else:
        upside_pct = stock.get("upside_pct", 8.0)
        downside_pct = max(abs(stock.get("downside_pct", -4.0)), 1.0)

    rr = upside_pct / downside_pct if downside_pct > 0 else 1.0
    rr_score = min(1.0, rr / 3.5)

    # LiquidityScore
    if amount > 0:
        liq = min(1.0, amount / 100000000.0)
        if turnover_pct > 0:
            liq = min(1.0, liq * 0.6 + (turnover_pct / 10.0) * 0.4)
    else:
        liq = 0.7 if data_source in ("fallback",) else 0.3

    # ExhaustionPenalty
    exhaustion = 0.2 if change_pct > 8 else 0.0
    if has_real_data and pct_5d > 25:
        exhaustion += 0.15  # 5日已大涨→衰竭风险

    # CrowdedPenalty
    crowdedness = min(1.0, (1 - rotation) * 0.5)
    crowded_penalty = crowdedness * 0.1

    # Confidence
    confidence = 0.5 + 0.3 * (fit + strength + rel) / 3
    if has_real_data:
        confidence = min(0.95, confidence + 0.1)

    return {
        "strategy_fit": fit,
        "sector_strength": strength,
        "relative_strength": rel,
        "volume_quality": vol_q,
        "momentum_quality": mom_q,
        "risk_reward": rr_score,
        "risk_reward_ratio": round(rr, 1),
        "liquidity_score": liq,
        "exhaustion_penalty": exhaustion,
        "crowded_penalty": crowded_penalty,
        "crowdedness": crowdedness,
        "confidence": round(confidence, 2),
    }


def _compute_oversold_reversal_components(
    change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra,
) -> dict:
    """超跌反弹策略评分：

    V2 使用真实 drawdown/ATR/5日跌幅判断超跌深度
    """
    regime = world.get("regime", "")
    regime_str = regime.value if hasattr(regime, 'value') else str(regime)
    rs = extra.get("relative_strength", 0.0)
    pct_5d = extra.get("pct_5d", 0.0)
    drawdown = extra.get("drawdown_pct", 0.0)
    atr_pct = extra.get("atr_pct", 0.0)
    volatility = extra.get("volatility", 0.0)
    data_source = extra.get("data_source", "fallback")
    turnover_pct = extra.get("turnover_pct", 0.0)
    has_real_data = data_source in ("warehouse", "realtime")

    # StrategyFit
    fit = 0.8 if regime_str in ("panic_reversal",) else (0.5 if sector_phase in ("ice_recovery",) else 0.3)

    # SectorStrength
    if not has_real_data and data_source in ("cache", "fallback"):
        if sector_phase in ("ice_recovery", "decay", "startup"):
            strength = 0.7
        elif sector_phase in ("main_rise_1", "acceleration", "high_divergence"):
            strength = 0.8
        else:
            strength = 0.5
    elif sector_phase in ("ice_recovery",):
        strength = 0.9
    elif sector_phase in ("decay",):
        strength = 0.5
    else:
        strength = 0.2

    # RelativeStrength：使用真实 drawdown + 5日跌幅
    if has_real_data:
        # 越深超跌越强
        t=0
        if rs < -40:
            rel = 0.85; t+=1
        elif rs < -20:
            rel = 0.75; t+=1
        elif rs < -5:
            rel = 0.6
        else:
            rel = 0.3

        # drawdown 修正
        if drawdown > 15:
            rel = min(0.9, rel + 0.1)
        elif drawdown > 8:
            rel = min(0.85, rel + 0.05)
    else:
        rel = 0.8 if change_pct < -4 else (0.6 if change_pct < -2 else 0.3)

    # VolumeQuality
    if vol_ratio > 1.8 and change_pct > 0:
        vol_q = 0.9    # 放量反包
    elif vol_ratio < 0.6:
        vol_q = 0.7    # 缩量企稳
    elif vol_ratio > 1.5:
        vol_q = 0.5
    else:
        vol_q = 0.4

    # MomentumQuality：波动率收缩信号
    mom_q = 0.7 if -5 <= change_pct <= -2 else 0.4
    if has_real_data and volatility > 0:
        # 波动率从高位下降 = 恐慌释放
        if volatility < 0.4:
            mom_q = max(mom_q, 0.75)

    # RiskReward：ATR/drawdown 估值
    if has_real_data and atr_pct > 0:
        upside_pct = atr_pct * 4
        downside_pct = max(atr_pct * 1.5, drawdown * 0.5 if drawdown > 0 else atr_pct)
    else:
        upside_pct = stock.get("upside_pct", 10.0)
        downside_pct = max(abs(stock.get("downside_pct", -5.0)), 1.0)

    rr = upside_pct / downside_pct if downside_pct > 0 else 1.0
    rr_score = min(1.0, rr / 3.0)

    # LiquidityScore
    if amount > 0:
        liq = min(1.0, amount / 100000000.0)
        if turnover_pct > 0:
            liq = min(1.0, liq * 0.6 + (turnover_pct / 10.0) * 0.4)
    else:
        liq = 0.7 if data_source in ("fallback",) else 0.3

    exhaustion = 0.0
    crowdedness = 0.3
    crowded_penalty = 0.05

    # Confidence
    confidence = 0.4 + 0.3 * (strength + rel) / 2
    if has_real_data:
        confidence = min(0.95, confidence + 0.1)

    return {
        "strategy_fit": fit,
        "sector_strength": strength,
        "relative_strength": rel,
        "volume_quality": vol_q,
        "momentum_quality": mom_q,
        "risk_reward": rr_score,
        "risk_reward_ratio": round(rr, 1),
        "liquidity_score": liq,
        "exhaustion_penalty": exhaustion,
        "crowded_penalty": crowded_penalty,
        "crowdedness": crowdedness,
        "confidence": round(confidence, 2),
    }


def _compute_generic_components(
    change_pct, vol_ratio, amount, sector_phase, is_leading, world, stock, extra,
) -> dict:
    """通用评分——默认 fallback"""
    fit = 0.5
    strength = 0.7 if sector_phase in ("main_rise_1", "acceleration", "startup", "high_divergence") else 0.5
    rel = 0.6 if change_pct >= 0 else 0.4
    vol_q = max(0.3, min(1.0, vol_ratio / 3.0))  # 保底0.3，防止全空数据得0分
    mom_q = 0.5
    upside_pct = stock.get("upside_pct", 5.0)
    downside_pct = max(abs(stock.get("downside_pct", -3.0)), 1.0)
    rr = upside_pct / downside_pct if downside_pct > 0 else 1.0
    rr_score = min(1.0, rr / 2.5)
    liq = min(1.0, amount / 100000000.0) if amount > 0 else 0.3
    exhaustion = 0.0
    crowdedness = 0.4
    crowded_penalty = 0.05
    confidence = 0.5

    return {
        "strategy_fit": fit,
        "sector_strength": strength,
        "relative_strength": rel,
        "volume_quality": vol_q,
        "momentum_quality": mom_q,
        "risk_reward": rr_score,
        "risk_reward_ratio": round(rr, 1),
        "liquidity_score": liq,
        "exhaustion_penalty": exhaustion,
        "crowded_penalty": crowded_penalty,
        "crowdedness": crowdedness,
        "confidence": confidence,
    }


# ═══════════════════════════════════════════
# 层级分类
# ═══════════════════════════════════════════

def _classify(score: float, confidence: float, risk_reward: float, crowdedness: float) -> str:
    """S/A/B/Watchlist 分级"""
    if score >= 80 and confidence >= 0.7 and risk_reward >= 2.0 and crowdedness < 0.6:
        return "S"
    elif score >= 60 and confidence >= 0.5 and risk_reward >= 1.5:
        return "A"
    elif score >= 35:
        return "B"
    else:
        return "Watchlist"


# ═══════════════════════════════════════════
# 理由生成
# ═══════════════════════════════════════════

def _build_reasons(strategy: str, comps: dict, sector_phase: str, change: float, vol: float, rr: float) -> list[str]:
    """生成结构化解释理由"""
    rs = []

    # 板块状态
    phase_map = {
        "startup": "启动期",
        "main_rise_1": "主升一期",
        "acceleration": "加速期",
        "high_divergence": "高位分歧",
        "decay": "退潮期",
        "ice_recovery": "冰点修复",
    }
    phase_cn = phase_map.get(sector_phase, sector_phase)
    strength = comps.get("sector_strength", 0)
    if strength >= 0.7:
        rs.append(f"板块处于{phase_cn}，趋势配合")
    elif strength >= 0.4:
        rs.append(f"板块{phase_cn}，具备操作空间")

    # 相对强度
    rel = comps.get("relative_strength", 0)
    if strategy in ("sector_rotation", "dip_stabilization"):
        if -3 <= change <= 0:
            rs.append("分歧回踩，企稳信号")
        elif 0 < change <= 3:
            rs.append("分歧中收阳，多头抵抗")
    elif strategy in ("trend_breakout",):
        if change > 3:
            rs.append(f"当日上涨{change:+.1f}%，突破信号")
        elif change > 0:
            rs.append("小幅走强，蓄势中")

    # 量能
    if vol > 1.5:
        if change > 0:
            rs.append("量能放大配合上涨")
        else:
            rs.append("放量调整，注意抛压")
    elif vol < 0.8:
        rs.append("缩量调整，浮筹出清")

    # 风险收益
    if rr >= 2.0:
        rs.append(f"风险收益比 {rr:.1f}，具备安全边际")

    # 置信度
    confidence = comps.get("confidence", 0.5)
    if confidence >= 0.7:
        rs.append("高置信度信号")
    elif confidence >= 0.5:
        rs.append("中等置信度")

    rs.append(f"策略匹配度 {comps.get('strategy_fit', 0)*100:.0f}%")

    return rs
