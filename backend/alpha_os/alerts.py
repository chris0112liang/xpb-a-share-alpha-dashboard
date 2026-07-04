"""
alpha_os/alerts.py — Alert Engine：市场事件/预警

职责：
  从 WorldState 中检测「值得关注」的市场事件。

规则全部基于 WorldState 字段 + Sector Lifecycle 数据。
不引入外部 API 调用。

事件类型：
  - SECTOR_TOPPING      板块加速→分歧（加速衰退）
  - PANIC_REVERSAL      冰点→修复（情绪拐点）
  - ROTATION_SPIKE      轮动速度极端
  - LEADER_BREAKDOWN    领涨板块退潮
  - ICE_POINT_RECOVERY  冰点修复信号
  - LIQUIDITY_WARNING   流动性风险

所有事件附带：
  severity / confidence / sector / description
"""

from __future__ import annotations

import logging
from typing import Any

from schemas.alpha_snapshot import MarketEvent

logger = logging.getLogger(__name__)


def detect_alerts(world_state: dict) -> list[MarketEvent]:
    """从 WorldState 检测市场事件

    支持多次调用——幂等，每次返回当前快照下的事件列表
    """
    events: list[MarketEvent] = []
    regime = world_state.get("regime", "")
    rotation = world_state.get("rotation_speed", 0.5)
    heatmap = world_state.get("sector_heatmap", {})
    lifecycles = world_state.get("lifecycles", {})

    # ── 外部感知中断判断 ──
    # 当 regime 为 init / 无 heatmap 数据时，预警引擎保持沉默
    if regime == "init" or not heatmap or not heatmap.get("phase_distribution"):
        if world_state.get("ai_summary", "").startswith("Alpha OS 正在初始化"):
            return events  # 静默
        # 有 cache 但 sector 数据仍为空 → 明确提示休眠
        events.append(MarketEvent(
            event_type="SENSOR_HIBERNATION",
            severity="info",
            confidence=1.0,
            description="外部感知中断，触发风控休眠——Relay 暂停候选股生成，等待数据链路恢复",
        ))
        return events

    _detect_rotation_spike(events, rotation, regime)
    _detect_sector_topping(events, heatmap, lifecycles)
    _detect_panic_reversal(events, heatmap, lifecycles)
    _detect_leader_breakdown(events, lifecycles, world_state.get("leading_sectors", []))
    _detect_ice_recovery(events, heatmap, lifecycles)

    return events


def _detect_rotation_spike(
    events: list[MarketEvent],
    rotation: float,
    regime: str,
):
    """轮动速度极端→ROTATION_SPIKE 事件"""
    if rotation > 0.85:
        events.append(MarketEvent(
            event_type="ROTATION_SPIKE",
            severity="warning",
            confidence=min(1.0, rotation),
            description=f"轮动速度{rotation:.2f}，资金高频切换，板块持续性极弱，追高风险大",
        ))
        logger.info(f"[Alert] ROTATION_SPIKE: {rotation:.2f}")
    elif rotation > 0.65 and regime in ("rotational_chop",):
        events.append(MarketEvent(
            event_type="ROTATION_SPIKE",
            severity="info",
            confidence=0.6,
            description=f"轮动速度{rotation:.2f}，板块快速轮动，适合低吸分歧策略",
        ))


def _detect_sector_topping(
    events: list[MarketEvent],
    heatmap: dict,
    lifecycles: dict,
):
    """加速→分歧衰退→SECTOR_TOPPING 事件

    从 turning_alerts 或 lifecycles 中检测 acceleration → high_divergence 的板块
    """
    turning = heatmap.get("turning_sectors", [])
    for entry in turning:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        signal = entry.get("signal", "")
        score = entry.get("score", 0)

        if signal == "accel_turning" and score > 50:
            events.append(MarketEvent(
                event_type="SECTOR_TOPPING",
                severity="warning",
                confidence=min(1.0, score / 100),
                sector=name,
                description=f"{name}板块加速期出现衰退拐点 (score={score:.0f})，注意阶段性见顶风险",
            ))

    # 也检查 lifecycles 中正在 high_divergence 的板块——已经有分歧
    # 注意：当外部感知中断时 lifecycles 为空，此循环自动跳过不生硬编
    for s_name, phase in lifecycles.items():
        if not isinstance(phase, str) and hasattr(phase, 'value'):
            phase_val = phase.value
        else:
            phase_val = str(phase)
        if phase_val in ("high_divergence",):
            events.append(MarketEvent(
                event_type="SECTOR_TOPPING",
                severity="info",
                confidence=0.5,
                sector=s_name,
                description=f"{s_name}板块处于高位分歧，控盘力量减弱，存在高低切风险",
            ))
            # 只取第一个作为信号——避免刷屏
            break


def _detect_panic_reversal(
    events: list[MarketEvent],
    heatmap: dict,
    lifecycles: dict,
):
    """冰点修复检测→PANIC_REVERSAL / ICE_POINT_RECOVERY 事件"""
    # 检测 ice_recovery → startup 的切换
    turning = heatmap.get("turning_sectors", [])
    for entry in turning:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        signal = entry.get("signal", "")

        if signal == "ice_recovery":
            events.append(MarketEvent(
                event_type="ICE_POINT_RECOVERY",
                severity="info",
                confidence=0.6,
                sector=name,
                description=f"{name}板块从冰点修复，资金开始介入修复",
            ))

    # 总体 regime 恐慌反转
    if heatmap.get("phase_distribution", {}).get("ice_recovery", 0) >= 3:
        events.append(MarketEvent(
            event_type="PANIC_REVERSAL",
            severity="warning",
            confidence=0.5,
            description=f"3个以上板块处于冰点修复阶段，市场情绪可能在酝酿反转",
        ))


def _detect_leader_breakdown(
    events: list[MarketEvent],
    lifecycles: dict,
    leading_sectors: list[str],
):
    """领涨板块出现衰退→LEADER_BREAKDOWN 事件"""
    for name in leading_sectors:
        phase = lifecycles.get(name, "")
        if not phase:
            continue
        phase_val = phase.value if hasattr(phase, 'value') else str(phase)
        if phase_val in ("decay", "high_divergence"):
            events.append(MarketEvent(
                event_type="LEADER_BREAKDOWN",
                severity="critical" if phase_val == "decay" else "warning",
                confidence=0.7 if phase_val == "decay" else 0.5,
                sector=name,
                description=f"领涨板块「{name}」进入{'退潮' if phase_val == 'decay' else '分歧'}，主线可能切换",
            ))
            # 一个就够
            break


def _detect_ice_recovery(
    events: list[MarketEvent],
    heatmap: dict,
    lifecycles: dict,
):
    """冰点修复汇总事件"""
    phase_dist = heatmap.get("phase_distribution", {})
    ice_count = phase_dist.get("ice_recovery", 0)

    if ice_count >= 2:
        ice_names = [
            n for n, p in lifecycles.items()
            if (hasattr(p, 'value') and p.value == "ice_recovery") or str(p) == "ice_recovery"
        ]
        events.append(MarketEvent(
            event_type="ICE_POINT_RECOVERY",
            severity="info",
            confidence=min(0.8, ice_count * 0.15),
            description=f"{ice_count}个板块处于冰点修复：{' '.join(ice_names[:3])}，超跌反弹窗口打开",
        ))
