"""
routes_alpha_os.py — Alpha OS 路由

原则：
  - 所有端点必须在 3 秒内返回
  - 外部 API 调用在后台刷新，不阻塞 HTTP
  - 进程级缓存优先
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter

from regime import get_regime_engine
from schemas import WorldState, RegimeState, MarketRegime, SignalGroup, AlphaScanReport
from strategy.selector import strategy_selector
from alpha.scanner import scan
from alpha.ranking import build_report
from alpha_os import AlphaBrain
from alpha_os.orchestrator import tick as _orchestrate_tick

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alpha", tags=["alpha-os"])

# ── 进程级缓存 ──
_CACHED_SNAPSHOT: dict = {}
_CACHED_AT: Optional[datetime] = None
_CACHED_LOCK = threading.Lock()
_REFRESHING = False
_REFRESH_INTERVAL_SEC = 20  # 每 20 秒后台刷新一次

# 模块加载时预填充最小快照（内联构造，不依赖延迟定义的函数）
_minimal_ts = datetime.now().strftime("%H:%M:%S")
_CACHED_SNAPSHOT.update({
    "ts": _minimal_ts,
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
    "ai_explanation": {"short": "系统启动中", "summary": "系统启动中", "detail": "组件初始化中"},
    "computed_at": _minimal_ts,
    "data_source": "init",
})
_CACHED_AT = datetime.now()


def _get_or_refresh() -> dict:
    """Return the current snapshot immediately and refresh in the background."""
    global _CACHED_SNAPSHOT, _CACHED_AT

    now = datetime.now()
    is_minimal = _CACHED_SNAPSHOT.get("regime") == "init"
    time_since_cache = (_CACHED_AT and (now - _CACHED_AT).total_seconds()) or 999

    if is_minimal or time_since_cache > _REFRESH_INTERVAL_SEC:
        _kick_background_refresh()

    return _CACHED_SNAPSHOT


def _kick_background_refresh():
    """Start one refresh worker and return immediately."""
    global _REFRESHING
    with _CACHED_LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True

    def _runner():
        global _REFRESHING
        try:
            _background_refresh()
        finally:
            with _CACHED_LOCK:
                _REFRESHING = False

    threading.Thread(target=_runner, daemon=True).start()

def _background_refresh():
    """后台异步 tick（永不抛异常、永不阻塞 HTTP）"""
    global _CACHED_SNAPSHOT, _CACHED_AT
    try:
        snap = _orchestrate_tick()
        with _CACHED_LOCK:
            # ★ 只要不是 None 就更新 —— 不允许永久卡在 init
            if snap is not None:
                _CACHED_SNAPSHOT = snap
                _CACHED_AT = datetime.now()
                is_init = snap.get("regime") == "init"
                status = snap.get('regime_display', 'init') if not is_init else snap.get('data_source', 'init')
                logger.info(f"[Terminal] 后台刷新: {status} | candidates={len(snap.get('top_candidates', []))}")
            else:
                logger.warning("[Terminal] _orchestrate_tick 返回 None")

    except Exception as e:
        logger.warning(f"[Terminal] 后台刷新失败: {e}")


def _build_minimal_snapshot() -> dict:
    """兜底快照——确保前端永远有数据可渲染"""
    now = datetime.now().strftime("%H:%M:%S")
    return {
        "ts": now,
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
        "ai_explanation": {"short": "系统启动中", "summary": "系统启动中", "detail": "组件初始化中"},
        "computed_at": now,
        "data_source": "init",
    }


# ── 端 点 ──


@router.get("/terminal")
async def get_alpha_terminal():
    """[统一终端] AlphaSnapshot

    永远 ≤ 3s 返回。后台异步刷新缓存。
    """
    snap = _get_or_refresh()
    return snap


@router.get("/world", response_model=WorldState)
async def get_world():
    """世界状态（系统大脑）"""
    snap = _get_or_refresh()
    ws = snap.get("world_state", {})
    # 尝试解析回 WorldState（尽量覆盖前端已有消费方）
    try:
        engine = get_regime_engine()
        world = engine.compute_world()
        return world
    except Exception:
        return ws


@router.get("/regime", response_model=RegimeState)
async def get_regime():
    """市场状态"""
    try:
        engine = get_regime_engine()
        regime = engine.compute_regime()
        return regime
    except Exception:
        snap = _get_or_refresh()
        regime_display = snap.get("regime_display", "未知")
        return RegimeState(
            regime=MarketRegime.ROTATIONAL_CHOP,
            confidence=0.0,
            dominant_style="MIXED",
            risk_level=0.5,
            signals=SignalGroup(breadth_score=0, volatility_score=0, turnover_score=0,
                                liquidity_score=0, momentum_dispersion=0,
                                sector_concentration=0, top_line_strength=0),
            state_model_explanation=f"缓存模式: {regime_display}",
        )


@router.get("/candidates", response_model=AlphaScanReport)
async def get_candidates(max: int = 20):
    """Alpha 扫描候选股"""
    snap = _get_or_refresh()

    # ═══════════════════════════════════════════════════════
    # ★ 硬风控：数据源不可用 → 物理截断归零
    # ═══════════════════════════════════════════════════════
    data_source = snap.get("data_source", "live")
    safe_live_sources = {"live", "realtime", "warehouse"}
    if data_source not in safe_live_sources:
        return AlphaScanReport(
            world_regime=snap.get("regime", "rotational_chop"),
            active_strategies=[],
            primary_strategy="风控休眠",
            total_candidates=0,
            candidates=[],
            scan_time=snap.get("computed_at", ""),
            explanation=f"硬风控触发：当前数据源为 {data_source}，未达到真实活跃行情标准，选股数量已物理截断归零",
        )

    candidates = snap.get("top_candidates", [])
    strategy = snap.get("active_strategies", ["sector_rotation"])
    primary = snap.get("primary_strategy", "sector_rotation")
    regime = snap.get("regime", "rotational_chop")

    return AlphaScanReport(
        world_regime=regime,
        active_strategies=strategy,
        primary_strategy=primary,
        total_candidates=len(candidates),
        candidates=candidates[:max],
        scan_time=snap.get("computed_at", ""),
        explanation="",
    )


@router.get("/strategy-select")
async def get_strategy_selector():
    """动态策略选择器"""
    snap = _get_or_refresh()
    return {
        "primary_strategy": snap.get("primary_strategy", "sector_rotation"),
        "active_strategies": snap.get("active_strategies", ["sector_rotation"]),
        "explanation": "AI 基于当前市场状态自动选择",
    }


@router.get("/world/health", include_in_schema=False)
async def world_health():
    """健康检查（永远快速返回）"""
    snap = _get_or_refresh()
    regime = snap.get("regime", "init")
    risk = snap.get("risk_level", 0.5)
    sectors = snap.get("leading_sectors", [])[:3]
    strategies = snap.get("active_strategies", [])[:3]
    ts = snap.get("computed_at", datetime.now().strftime("%H:%M:%S"))
    return {
        "status": "healthy" if regime != "init" else "starting",
        "regime": regime,
        "risk_level": risk,
        "hot_sectors": sectors,
        "active_strategies": strategies,
        "timestamp": ts,
    }
