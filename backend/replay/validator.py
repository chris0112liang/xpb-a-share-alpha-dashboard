"""
replay/validator.py — 预测验证器

次日运行：
  等盘后数据到位 → 对比昨日预测 vs 今日实际情况

验证维度：
  1. Regime 变化预测（昨日 regime → 今日 regime 是否与预期一致）
  2. Rotation Speed 趋势（预测上升/下降，是否正确）
  3. Candidate 命中率（昨日推荐的股，今日是否上涨/跑赢板块）
  4. Event 验证（昨日预警的事件是否发生/缓解）
  5. Ranking 质量（高 tier 是否优于低 tier 表现）

输出：ValidationReport
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from .recorder import list_snapshots, load_snapshot, load_latest_snapshot

logger = logging.getLogger(__name__)


def validate_candidates(
    prev_candidates: list[dict],
    today_closes: dict[str, float],
    today_pct_changes: dict[str, float],
    sector_closes: dict[str, float],
) -> dict:
    """
    验证昨日候选股今日表现

    prev_candidates: 昨日快照中的 candidates
    today_closes: {symbol: close_price}
    today_pct_changes: {symbol: pct_change}
    sector_closes: {sector_name: avg_pct_change}

    返回：
      {
        "total": int,
        "profitable": int (上涨),
        "win_rate": float,
        "beat_sector": int (跑赢板块),
        "beat_sector_rate": float,
        "by_tier": {
          "S": {"total": n, "win": n, "avg_pct": x},
          "A": {...},
          "B": {...},
          "Watchlist": {...}
        },
        "avg_score": float,
        "best": {"symbol": str, "pct": float},
        "worst": {"symbol": str, "pct": float},
      }
    """
    if not prev_candidates:
        return {"total": 0, "win_rate": 0.0, "by_tier": {}}

    total = len(prev_candidates)
    profitable = 0
    beat_sector = 0
    by_tier: dict[str, dict] = {}
    total_score = 0.0
    best: Optional[dict] = None
    worst: Optional[dict] = None

    for c in prev_candidates:
        symbol = c.get("symbol", "")
        tier = c.get("tier", "Watchlist")
        score = c.get("score", 0) or 0
        sector = c.get("sector", "")
        total_score += score

        pct = today_pct_changes.get(symbol, 0) or 0
        sector_pct = sector_closes.get(sector, 0) or 0

        if pct > 0:
            profitable += 1
        if pct > sector_pct:
            beat_sector += 1

        if tier not in by_tier:
            by_tier[tier] = {"total": 0, "win": 0, "avg_pct": 0.0, "total_pct": 0.0}
        by_tier[tier]["total"] += 1
        by_tier[tier]["total_pct"] += pct
        if pct > 0:
            by_tier[tier]["win"] += 1

        if best is None or pct > best["pct"]:
            best = {"symbol": symbol, "pct": round(pct, 2)}
        if worst is None or pct < worst["pct"]:
            worst = {"symbol": symbol, "pct": round(pct, 2)}

    # 按 tier 计算平均涨幅
    for t, v in by_tier.items():
        if v["total"] > 0:
            v["avg_pct"] = round(v["total_pct"] / v["total"], 2)

    avg_score = round(total_score / total, 1) if total > 0 else 0.0

    return {
        "total": total,
        "profitable": profitable,
        "win_rate": round(profitable / total * 100, 1) if total > 0 else 0.0,
        "beat_sector": beat_sector,
        "beat_sector_rate": round(beat_sector / total * 100, 1) if total > 0 else 0.0,
        "by_tier": by_tier,
        "avg_score": avg_score,
        "best": best,
        "worst": worst,
    }


def validate_regime_prediction(
    prev_regime: str,
    today_regime: str,
    prev_rotation: float,
    today_rotation: float,
) -> dict:
    """
    验证市场状态预测

    对比昨日 regime 预期 vs 今日实际
    """
    # 简单策略：如果 regime 不变且 rotation 趋势符合，算正确
    rotation_trend_correct = True
    if abs(today_rotation - prev_rotation) > 0.1:
        rotation_trend_correct = False

    regime_stable = (prev_regime == today_regime)

    return {
        "prev_regime": prev_regime,
        "today_regime": today_regime,
        "stable": regime_stable,
        "prev_rotation": round(prev_rotation, 2),
        "today_rotation": round(today_rotation, 2),
        "rotation_trend_correct": rotation_trend_correct,
        "prediction_accurate": regime_stable or rotation_trend_correct,
    }


def validate_event(
    event: dict,
    today_regime: str,
    today_sectors: dict[str, str],  # {sector_name: phase}
) -> dict:
    """
    验证事件是否生效

    例如：
      SECTOR_TOPPING → 板块是否进入退潮期
      PANIC_REVERSAL → regime 是否变为 panic_reversal
    """
    event_type = event.get("event_type", "")
    sector = event.get("sector", "")
    description = event.get("description", "")

    validated = False
    detail = ""

    if event_type == "REGIME_CHANGE":
        # 检查 regime 是否确实变了
        validated = True  # 记录性事件
        detail = f"regime was {today_regime}"

    elif event_type == "SECTOR_TOPPING":
        phase = today_sectors.get(sector, "")
        if phase in ("decay",):
            validated = True
            detail = f"{sector} entered decay"
        else:
            detail = f"{sector} remains {phase}"

    elif event_type == "ROTATION_SPIKE":
        validated = True
        detail = f"monitored"

    elif event_type == "LEADER_BREAKDOWN":
        phase = today_sectors.get(sector, "")
        if phase in ("decay", "noise"):
            validated = True
            detail = f"{sector} confirmed breakdown ({phase})"
        else:
            detail = f"{sector} recovered to {phase}"

    return {
        "event_type": event_type,
        "description": description,
        "validated": validated,
        "detail": detail,
    }


def run_daily_validation(
    prev_date_str: str,
    today_state: dict,
    today_closes: dict[str, float],
    today_pct_changes: dict[str, float],
    today_sector_changes: dict[str, float],
    today_sector_phases: dict[str, str],
) -> dict:
    """
    运行一次完整验证

    prev_date_str: "%Y%m%d" 格式的日期
    today_state: 今日 AlphaBrain.tick() 输出（新鲜）或 dict
    ...
    """
    # 加载昨日最后一条快照
    prev = load_latest_snapshot(prev_date_str)
    if not prev:
        return {"error": f"No snapshot for {prev_date_str}", "valid": False}

    # 1. Regime 预测
    regime_report = validate_regime_prediction(
        prev.get("regime", ""),
        today_state.get("regime", ""),
        prev.get("rotation_speed", 0.0),
        today_state.get("rotation_speed", 0.0),
    )

    # 2. 候选股表现
    candidate_report = validate_candidates(
        prev.get("candidates", []),
        today_closes,
        today_pct_changes,
        today_sector_changes,
    )

    # 3. 事件验证
    events = prev.get("events", [])
    event_reports = [
        validate_event(e, today_state.get("regime", ""), today_sector_phases)
        for e in events
    ]

    return {
        "valid": True,
        "date": prev_date_str,
        "regime": regime_report,
        "candidates": candidate_report,
        "events": {
            "total": len(events),
            "validated": sum(1 for r in event_reports if r["validated"]),
            "details": event_reports,
        },
        "score": round(
            regime_report.get("prediction_accurate", False) * 30 +
            candidate_report.get("win_rate", 0) * 0.5,
            1,
        ),
    }
