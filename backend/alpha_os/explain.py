"""
alpha_os/explain.py — Explain Engine：结构化 AI 解释

职责：
  从 WorldState + StrategyResult + Events 生成「市场认知」解释。

不是：
  "MACD 金叉" / "RSI 超卖"

而是：
  "半导体维持主线强度，rotation 0.72 表明资金高速轮动"
  "当前适合低吸分歧，不适合追高加速"
  "钢铁板块出现首次加速衰退，存在阶段性高低切风险"

全结构化，不依赖 LLM。
"""

from __future__ import annotations

import logging
from typing import Any

from schemas.alpha_snapshot import AiExplanation, MarketEvent

logger = logging.getLogger(__name__)

# 市场状态中文名
REGIME_CN = {
    "bull_trend": "多头趋势",
    "bear_trend": "空头趋势",
    "rotational_chop": "轮动震荡",
    "high_volatility": "高波动",
    "panic_reversal": "恐慌反转",
}

# 策略中文名
STRATEGY_CN = {
    "trend_breakout": "趋势突破",
    "sector_rotation": "板块轮动",
    "dip_stabilization": "分歧低吸",
    "dip_reversal": "分歧低吸",
    "oversold_reversal": "超跌反弹",
    "defensive_cash": "空仓防御",
    "cash_defense": "空仓防御",
}


def build_explanation(
    world_state: dict,
    strategy_result: dict,
    events: list[MarketEvent],
    candidates: list[Any],
    top_candidates: list[Any] | None = None,
) -> AiExplanation:
    """构建结构化 AI 解释"""
    tc = top_candidates or candidates

    regime = world_state.get("regime", "")
    regime_cn = REGIME_CN.get(
        regime.value if hasattr(regime, 'value') else str(regime),
        str(regime),
    )
    rotation = world_state.get("rotation_speed", 0.5)
    leading = world_state.get("leading_sectors", [])
    heatmap = world_state.get("sector_heatmap", {})

    active = strategy_result.get("active_strategies", [])
    primary = strategy_result.get("primary_strategy", "")
    primary_cn = STRATEGY_CN.get(primary, primary)

    # ── 外部感知中断检查 ──
    if regime == "init" or (not events and not candidates):
        return AiExplanation(
            regime="系统初始化",
            rotation_analysis="数据积累中",
            risk_assessment="外部感知中断，触发风控休眠",
            strategy_rationale="等待数据链路恢复",
            leading_sector_insight="—",
            candidate_explanation="Relay 暂停候选股生成",
            warning="⚠️ 外部感知中断，触发风控休眠——Relay 暂停候选股生成，等待数据链路恢复",
            raw_text="当前外部市场感知中断，Relay 系统已进入风控休眠模式。待数据链路恢复后自动脱离休眠。",
        )

    if not candidates:
        return AiExplanation(
            regime=regime_cn,
            rotation_analysis=_describe_rotation(rotation, leading),
            risk_assessment=f"风险等级{world_state.get('risk_level',0.5):.2f}",
            strategy_rationale=_describe_strategy(primary_cn, active, world_state),
            leading_sector_insight=_describe_leading(leading, heatmap),
            candidate_explanation="当前无符合条件的候选股",
            warning=_describe_events(events),
            raw_text=f"市场{regime_cn}，轮动速度{rotation:.2f}。无候选股。",
        )

    # ── 正常解释构建 ──
    rotation_text = _describe_rotation(rotation, leading)
    risk_text = f"风险等级{world_state.get('risk_level',0.5):.2f}"
    strategy_text = _describe_strategy(primary_cn, active, world_state)
    leading_text = _describe_leading(leading, heatmap)
    candidate_text = _describe_candidates(tc, heatmap)
    warning_text = _describe_events(events)

    raw = (
        f"市场{regime_cn}，轮动速度{rotation:.2f}。\n"
        f"{rotation_text}\n"
        f"{risk_text}\n"
        f"{strategy_text}\n"
        f"{leading_text}\n"
        f"{candidate_text}\n"
        f"{warning_text}"
    ).strip()

    return AiExplanation(
        regime=regime_cn,
        rotation_analysis=rotation_text,
        risk_assessment=risk_text,
        strategy_rationale=strategy_text,
        leading_sector_insight=leading_text,
        candidate_explanation=candidate_text[:200],
        warning=warning_text,
        raw_text=raw,
    )


def _describe_regime(regime, regime_cn: str, rotation: float, risk: float) -> str:
    """市场状态描述"""
    m = {
        "bull_trend": "上涨趋势明确，宽度好，适合积极操作",
        "bear_trend": "下跌趋势，宽度差，注意风险控制",
        "rotational_chop": "板块快速轮动，方向不确定性高，不建议追高",
        "high_volatility": "大起大落，仓位偏保守，等待方向确认",
        "panic_reversal": "极端情绪后反转，关注超跌反弹机会",
    }
    base = m.get(
        regime.value if hasattr(regime, 'value') else str(regime),
        f"市场{regime_cn}状态",
    )
    return f"当前{regime_cn}——{base}"


def _describe_rotation(rotation: float, leading: list[str]) -> str:
    """轮动状态解释"""
    if rotation > 0.8:
        return f"轮动速度{rotation:.2f}——资金高速切换，板块持续性极弱"
    elif rotation > 0.6:
        return f"轮动速度{rotation:.2f}——资金中高速轮动，板块有短期机会但持续性有限"
    elif rotation > 0.4:
        return f"轮动速度{rotation:.2f}——正常板块轮动，主线相对稳定"
    else:
        msg = f"轮动速度{rotation:.2f}——板块轮动缓慢，"
        if leading:
            msg += f"主线锁定「{' '.join(leading[:2])}」"
        else:
            msg += "无明显主线"
        return msg


def _describe_risk(risk: float, regime) -> str:
    """风险评估"""
    if risk > 0.7:
        return f"风险等级{risk:.2f}——高，注意仓位控制和止损纪律"
    elif risk > 0.45:
        return f"风险偏好{risk:.2f}——适中，保持灵活仓位"
    else:
        return f"风险偏好{risk:.2f}——低，适合积极布局"


def _describe_strategy(primary_cn: str, active: list[str], world: dict) -> str:
    """策略选择理由"""
    if not active:
        return "当前无匹配策略"

    active_cn = [STRATEGY_CN.get(s, s) for s in active]
    parts = [f"当前适合{' / '.join(active_cn)}"]

    # 加入具体方向
    if "sector_rotation" in active or "dip_stabilization" in active:
        parts.append("注意低吸分歧，不追高加速")
    if "trend_breakout" in active:
        parts.append("关注放量突破和主升趋势")
    if "oversold_reversal" in active:
        parts.append("关注超跌反弹和冰点修复机会")
    if "defensive_cash" in active or "cash_defense" in active:
        parts.append("以防守为主，控制仓位")

    return "。".join(parts)


def _describe_leading(leading: list[str], heatmap: dict) -> str:
    """领涨板块洞察"""
    if not leading:
        return "当前无明显领涨板块，资金分散"

    turning = heatmap.get("turning_sectors", [])
    turning_names = {t.get("name", "") for t in turning if isinstance(t, dict)}

    # 检查领涨板块是否有拐点
    at_risk = [n for n in leading if n in turning_names]
    if at_risk:
        return f"领涨板块「{' '.join(leading[:3])}」，但{' '.join(at_risk)}出现衰退信号"
    else:
        return f"主线集中在「{' '.join(leading[:3])}」，维持当前强度"


def _describe_candidates(candidates: list, heatmap: dict) -> str:
    """候选股解释"""
    if not candidates:
        return "当前无符合条件的候选股"

    by_strategy: dict[str, list] = {}
    for c in candidates:
        s = getattr(c, 'triggered_strategy_display', None) or getattr(c, 'triggered_strategy', '?')
        by_strategy.setdefault(s, []).append(c)

    parts = []
    for s_name, group in sorted(by_strategy.items()):
        top = group[0]
        top_name = getattr(top, 'name', '?')
        top_score = getattr(top, 'score', 0)
        parts.append(f"【{s_name}】{len(group)}只候选，最强{top_name}评分{top_score:.0f}")

    return f"已扫描{' '.join(parts)}"


def _describe_events(events: list[MarketEvent]) -> str:
    """预警汇总"""
    if not events:
        return ""

    critical = [e for e in events if e.severity == "critical"]
    warnings = [e for e in events if e.severity == "warning"]
    infos = [e for e in events if e.severity == "info"]

    parts = []
    for e in critical:
        parts.append(f"⚠️ 严重: {e.description}")
    for e in warnings[:2]:
        parts.append(f"⚡ {e.description}")
    for e in infos[:2]:
        parts.append(f"ℹ️ {e.description}")

    return " | ".join(parts) if parts else ""
