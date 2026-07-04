"""
alpha_os/orchestrator.py — AlphaBrain 内部编排逻辑

包含 tick() 核心实现，按步骤执行：
  1. 更新 WorldState
  2. 更新 Sector Lifecycle
  3. 更新 Strategy Selector
  4. 更新 Alpha Scanner
  5. 检测市场事件
  6. 生成 AI 解释
  7. 输出 AlphaSnapshot
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from regime import get_regime_engine
from strategy.selector import strategy_selector
from alpha.scanner import scan
from alpha.ranking import build_report
from alpha.market_filter import fetch_market_snapshot
from alpha_os import alerts, explain, memory
from replay.recorder import record_snapshot

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


def tick() -> dict:
    """AlphaBrain 主心跳——执行一次完整的大脑循环

    返回：
      完整的 AlphaSnapshot 序列化字典
    永远不阻塞超过 5 秒，超出则返回上次缓存。
    """
    mem = memory.get_memory()
    start_ts = datetime.now()
    HARD_TIMEOUT_SEC = 90  # 90 秒硬超时（第一次 tick 需要时间启动 + 市场快照 15-20s）

    # ── Step 1: 获取 WorldState（带超时 30s） ──
    world_dict = None
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(lambda: get_regime_engine().compute_world())
            world = fut.result(timeout=30.0)
            world_dict = _serialize_world(world)
    except concurrent.futures.TimeoutError:
        elapsed = (datetime.now() - start_ts).total_seconds()
        logger.warning(f"[Brain] compute_world 超时 ({elapsed:.1f}s)")
    except Exception as e:
        elapsed = (datetime.now() - start_ts).total_seconds()
        logger.warning(f"[Brain] compute_world 失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")

    if world_dict is None:
        # 返回上次缓存
        cached = getattr(tick, "_cached", None)
        if cached:
            return cached
        # 首次启动无缓存，返回最小结构
        return _minimal_snapshot()

    # ── Step 2: 更新 Strategy ──
    try:
        strategy_result = strategy_selector(world_dict)
    except Exception as e:
        logger.warning(f"[Brain] strategy_selector 失败: {e}")
        strategy_result = {"active_strategies": ["sector_rotation"], "primary_strategy": "sector_rotation", "explanation": "策略选择器降级"}

    # ── Step 3: 获取市场快照 + Alpha 扫描 ──
    market_snapshot = []
    candidates: list = []
    try:
        elapsed = (datetime.now() - start_ts).total_seconds()
        if elapsed < HARD_TIMEOUT_SEC:
            # 市场快照直接调用（不套额外线程池，避免嵌套死锁）
            # 函数内部已有自己的线程池 + 超时
            market_snapshot = fetch_market_snapshot(1500)
            # 即使快照为空也继续 scan（scan 内部处理空情况）
            candidates = scan(world_dict, strategy_result, market_snapshot, max_candidates=20)
        else:
            logger.warning("[Brain] 跳过 market_snapshot (超时)")
    except Exception as e:
        logger.warning(f"[Brain] 扫描阶段失败: {e}")

    # ── Step 4: 检测市场事件 ──
    events = alerts.detect_alerts(world_dict)

    # 去重（跳过已经发过的事件）
    deduped_events = [
        e for e in events
        if not mem.has_seen_event(e.event_type, e.sector, e.severity)
    ]

    # ── Step 5: 检查 Regime 变化（注册为事件） ──
    changed, change_desc = mem.regime_changed(world_dict.get("regime", ""))
    if changed:
        from schemas.alpha_snapshot import MarketEvent
        deduped_events.append(MarketEvent(
            event_type="REGIME_CHANGE",
            severity="warning",
            confidence=0.8,
            description=f"市场状态切换：{change_desc}",
        ))

    # ── Step 6: 构建 AI 解释 ──
    ai_explanation = explain.build_explanation(
        world_state=world_dict,
        strategy_result=strategy_result,
        events=deduped_events,
        candidates=candidates,
    )

    # ── Step 7: 组装 AlphaSnapshot ──
    regime_raw = world_dict.get("regime", "")
    regime_str = regime_raw.value if hasattr(regime_raw, 'value') else str(regime_raw)
    regime_cn = REGIME_CN.get(regime_str, regime_str)

    now = datetime.now()
    now_str = now.strftime("%H:%M:%S")

    # 交易时间标记
    if 9 <= now.hour < 11 or 13 <= now.hour < 15:
        time_status = "交易中"
    else:
        time_status = "盘后"

    # 计算市场偏向
    breadth = world_dict.get("breadth_score", 0.0)
    momentum = world_dict.get("momentum_score", 0.0)
    market_bias = (breadth + momentum) / 2

    # 候选股 → dict
    candidates_dict = [c.to_dict() if hasattr(c, 'to_dict') else c for c in candidates]

    # 事件 → dict
    events_dict = [e.model_dump() if hasattr(e, 'model_dump') else e.__dict__ for e in deduped_events]

    # 取拐点预警
    heatmap = world_dict.get("sector_heatmap", {})
    turning_alerts = heatmap.get("turning_sectors", [])

    # 构建 summary
    primary = strategy_result.get("primary_strategy", "")
    primary_cn = STRATEGY_CN.get(primary, primary)
    active_strategies = strategy_result.get("active_strategies", [])

    if deduped_events:
        severe = [e for e in deduped_events if e.severity in ("critical", "warning")]
        event_summary = f" | 触发{len(severe)}条预警" if severe else ""
    else:
        event_summary = ""

    ai_summary = (
        f"{time_status} {regime_cn} {primary_cn} "
        f"轮动{world_dict.get('rotation_speed', 0.5):.2f} "
        f"领涨{'/'.join(world_dict.get('leading_sectors', [])[:3])} "
        f"候选{len(candidates)}只{event_summary}"
    )

    # ★ 检测数据源状态：全 fallback → 标记为风控休眠
    total_live = sum(1 for s in market_snapshot if not s.get("is_fallback", False) and s.get("price", 0) > 0)
    data_source_status = "fallback" if total_live == 0 else "live"

    snapshot = {
        "ts": now_str,
        "world_state": world_dict,
        "regime": regime_str,
        "regime_display": regime_cn,
        "active_strategies": active_strategies,
        "primary_strategy": primary,
        "leading_sectors": world_dict.get("leading_sectors", []),
        "rotation_speed": world_dict.get("rotation_speed", 0.5),
        "risk_level": world_dict.get("risk_level", 0.5),
        "market_bias": round(market_bias, 3),
        "turning_alerts": turning_alerts,
        "top_candidates": candidates_dict[:10] if data_source_status == "live" else (candidates_dict[:10] if candidates_dict else []),
        "market_events": events_dict,
        "ai_summary": ai_summary if data_source_status == "live" else f"{time_status} {regime_cn} — 盘后数据",
        "ai_explanation": ai_explanation.model_dump() if hasattr(ai_explanation, 'model_dump') else ai_explanation.__dict__,
        "computed_at": now_str,
        "data_source": data_source_status,
    }

    # 记录到运行记忆 + Replay 持久化
    mem.record(snapshot)
    _persist_replay(snapshot)

    # 写入进程级缓存
    tick._cached = snapshot
    return snapshot


def _minimal_snapshot() -> dict:
    """兜底最小快照——确保首次启动时前端有数据"""
    now_str = datetime.now().strftime("%H:%M:%S")
    snap = {
        "ts": now_str,
        "world_state": {},
        "regime": "init",
        "regime_display": "系统初始化",
        "active_strategies": ["init"],
        "primary_strategy": "init",
        "leading_sectors": [],
        "rotation_speed": 0.5,
        "risk_level": 0.5,
        "market_bias": 0.5,
        "turning_alerts": [],
        "top_candidates": [],
        "market_events": [],
        "ai_summary": "Alpha OS 正在初始化...",
        "ai_explanation": {"short": "系统启动中，首轮数据获取进行中", "summary": "系统启动中", "detail": "组件正在初始化"},
        "computed_at": now_str,
        "data_source": "init",
    }
    tick._cached = snap
    return snap


def _serialize_world(world) -> dict:
    """将 WorldState 模型安全序列化为 dict"""
    if hasattr(world, 'model_dump'):
        wd = world.model_dump()
    elif hasattr(world, 'dict'):
        wd = world.dict()
    else:
        wd = world.__dict__

    # 确保 lifecycles 中的枚举转字符串
    lifecycles = wd.get("lifecycles", {})
    serialized_lc = {}
    for name, phase in lifecycles.items():
        serialized_lc[name] = phase.value if hasattr(phase, 'value') else str(phase)
    wd["lifecycles"] = serialized_lc

    # regime 枚举转字符串
    regime = wd.get("regime", "")
    wd["regime"] = regime.value if hasattr(regime, 'value') else str(regime)

    # leading_sectors 确保列表
    if not isinstance(wd.get("leading_sectors"), list):
        wd["leading_sectors"] = []

    return wd


def _persist_replay(snapshot: dict) -> None:
    """自动写入 Replay 快照——永不让写入错误中断主循环"""
    try:
        record_snapshot(snapshot)
    except Exception as e:
        logger.warning(f"[Replay] 自动写入失败 (非致命): {e})")

# 模块加载时预缓存最小快照
_minimal_snapshot()
