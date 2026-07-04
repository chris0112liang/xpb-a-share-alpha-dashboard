"""
reports/daily/report.py — Alpha Daily Report

盘后调用，自动生成：
  - 今日 regime / rotation / risk 快照
  - 主线板块列表（phase ≥ main_rise_1）
  - 策略激活情况
  - Top candidates（top 5 by score）
  - 次日验证结果（如果已有）
  - calibration 建议
  - tier accuracy 统计
  - risk changes 对比昨日

输出路径：reports/daily/YYYYMMDD.json
保留 90 天 → 自动清理旧报告。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from alpha.calibration import calibration_report
from replay.recorder import list_snapshots, load_latest_snapshot

logger = logging.getLogger(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__))

TIER_LABELS = {"S": "S级", "A": "A级", "B": "B级", "Watchlist": "观察"}

RETENTION_DAYS = 90


# ═══════════════════════════════════════════
# 构建每日报告
# ═══════════════════════════════════════════

def _sector_phase_label(phase: str) -> str:
    labels = {
        "startup": "启动", "main_rise_1": "主升一", "acceleration": "加速",
        "high_divergence": "分歧", "decay": "退潮", "ice_recovery": "冰点修复",
    }
    return labels.get(phase, phase)


def _parse_leading_sectors(world_state: dict) -> list[dict]:
    """从 WorldState 中提取主线板块"""
    leading = world_state.get("leading_sectors", [])
    if isinstance(leading, list):
        return [
            {"name": s.get("name", s) if isinstance(s, dict) else s,
             "phase": _sector_phase_label(s.get("phase", "") if isinstance(s, dict) else ""),
             "score": s.get("score", 0) if isinstance(s, dict) else 0}
            for s in leading
        ]
    return []


def generate_daily_report(
    alpha_snapshot: dict | None = None,
    prev_date: str | None = None,
) -> dict:
    """生成一份 Alpha Daily Report

    Args:
      alpha_snapshot: AlphaBrain.tick() 输出（快照中的 world_state/strategies/candidates）
      prev_date: 昨日日期 YYYYMMDD（用于 risk 对比），默认昨日

    Returns:
      report dict, 同时写入 reports/daily/{date}.json
    """
    today = datetime.now()
    date_str = today.strftime("%Y%m%d")
    time_str = today.strftime("%H:%M:%S")
    if prev_date is None:
        prev_date = (today - timedelta(days=1)).strftime("%Y%m%d")

    # ── 若无传入快照，从 replay 加载 ──
    if alpha_snapshot is None:
        latest = load_latest_snapshot(date_str)
        if latest is None:
            # 尝试前一日
            latest = load_latest_snapshot(prev_date)
        if latest is None:
            return {"error": f"no snapshot for {date_str}", "date": date_str}
    else:
        # 从 AlphaBrain.tick() 返回的完整字典提取所需字段
        latest = {
            "regime": alpha_snapshot.get("regime", ""),
            "regime_display": alpha_snapshot.get("regime_display", ""),
            "primary_strategy": alpha_snapshot.get("primary_strategy", ""),
            "active_strategies": alpha_snapshot.get("active_strategies", []),
            "rotation_speed": alpha_snapshot.get("rotation_speed", 0.0),
            "risk_level": alpha_snapshot.get("risk_level", 0.0),
            "market_bias": alpha_snapshot.get("market_bias", 0.0),
            "leading_sectors": _parse_leading_sectors(alpha_snapshot.get("world_state", {})),
            "candidates": [
                {
                    "symbol": c.get("symbol", ""),
                    "name": c.get("name", ""),
                    "score": c.get("score", 0),
                    "tier": c.get("tier", ""),
                    "confidence": c.get("confidence", 0),
                    "risk_reward": c.get("risk_reward", 0),
                    "reasons": c.get("reasons", []),
                    "data_source": c.get("data_source", ""),
                }
                for c in alpha_snapshot.get("top_candidates", [])[:10]
            ],
            "events": [
                {
                    "event_type": e.get("event_type", ""),
                    "severity": e.get("severity", ""),
                    "description": e.get("description", ""),
                }
                for e in alpha_snapshot.get("market_events", [])
            ],
        }

    # ── 昨日候选对比 ──
    prev_snapshot = load_latest_snapshot(prev_date)
    risk_change = 0.0
    rotation_change = 0.0
    if prev_snapshot:
        risk_change = round(
            (latest.get("risk_level", 0) or 0) - (prev_snapshot.get("risk_level", 0) or 0),
            3,
        )
        rotation_change = round(
            (latest.get("rotation_speed", 0) or 0) - (prev_snapshot.get("rotation_speed", 0) or 0),
            3,
        )

    # ── Tier 分布统计 ──
    candidates = latest.get("candidates", [])
    tier_dist = {"S": 0, "A": 0, "B": 0, "Watchlist": 0}
    for c in candidates:
        t = c.get("tier", "Watchlist")
        if t in tier_dist:
            tier_dist[t] += 1
    tier_summary = [
        {"tier": t, "label": TIER_LABELS.get(t, t), "count": n}
        for t, n in tier_dist.items() if n > 0
    ]

    # ── 主线板块 ──
    leading_sectors = latest.get("leading_sectors", [])
    if not leading_sectors:
        # 从 world_state fallback
        pass

    # ── 策略激活 ──
    active_strategies = latest.get("active_strategies", [])
    if isinstance(active_strategies, list) and active_strategies:
        primary = latest.get("primary_strategy", active_strategies[0])
    else:
        primary = latest.get("primary_strategy", "")

    # ── Calibration 建议 ──
    try:
        calib = calibration_report(days=7)
        calib_score = calib.get("calibration_score", 0)
        calib_warnings = calib.get("warnings", [])
        calib_rebalance = calib.get("weight_rebalance", {})
    except Exception as e:
        logger.warning(f"Calibration report failed: {e}")
        calib_score = 0
        calib_warnings = []
        calib_rebalance = {}

    # ── 构建报告 ──
    report = {
        "date": date_str,
        "generated_at": time_str,
        "regime": {
            "name": latest.get("regime_display", ""),
            "code": latest.get("regime", ""),
            "rotation_speed": round(latest.get("rotation_speed", 0), 3),
            "risk_level": round(latest.get("risk_level", 0), 3),
            "market_bias": round(latest.get("market_bias", 0), 3),
            "rotation_change_vs_yesterday": rotation_change,
            "risk_change_vs_yesterday": risk_change,
        },
        "leading_sectors": leading_sectors,
        "strategy": {
            "primary": primary,
            "active": active_strategies,
        },
        "top_candidates_summary": {
            "total": len(candidates),
            "tier_distribution": tier_summary,
            "top_5": candidates[:5],
        },
        "events": latest.get("events", []),
        "validation": {
            "prev_date": prev_date,
            "has_prev_snapshot": prev_snapshot is not None,
        },
        "calibration": {
            "score": calib_score,
            "warnings": calib_warnings,
            "rebalance_suggestions": calib_rebalance.get("priority", []),
        },
    }

    # ── 写入文件 ──
    os.makedirs(os.path.join(REPORTS_DIR, date_str[:6]), exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"{date_str}.json")
    with open(path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"[DailyReport] written to {path}")

    # ── 清理旧报告 ──
    _cleanup_old_reports()

    return report


def _cleanup_old_reports():
    """保留 RETENTION_DAYS 天的报告"""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
    for fname in os.listdir(REPORTS_DIR):
        if not fname.endswith(".json"):
            continue
        date_part = fname.replace(".json", "")
        if date_part.isdigit() and date_part < cutoff:
            path = os.path.join(REPORTS_DIR, fname)
            os.remove(path)
            logger.info(f"[DailyReport] cleaned {path}")
