"""
alpha/calibration.py — Ranking Calibration System

闭环：record → validate → recalibrate

目标：
  每运行一次 daily validation，输出 CalibrationReport。
  报告包含：
  - tier_accuracy:    S/A/B/Watchlist 的命中率分层
  - strategy_winrate: 各策略近 N 日胜率
  - regime_validation: regime/rotation 预测准确率
  - weight_rebalance: 建议调整的 Ranking V2 权重
  - calibration_report: 完整报告（JSON，也是唯一输出入口）

不自动调整权重（只输出建议）。
不引入 RL。
不启动任何后台循环。
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 阈值（可外部覆盖） ──
TIER_WIN_THRESHOLDS = {
    "S": 0.65,   # S 级应 ≥ 65% 胜率
    "A": 0.50,   # A 级应 ≥ 50% 胜率
    "B": 0.35,   # B 级应 ≥ 35% 胜率
}
TIER_BEAT_SECTOR_THRESHOLDS = {
    "S": 0.50,
    "A": 0.40,
    "B": 0.25,
}

STRATEGY_WIN_THRESHOLD = 0.40  # 低于此值的策略需调参

REGIME_SWITCH_SCORE = 30.0     # regime 预测正确得 30 分
REGIME_STABLE_PENALTY = -10.0  # 错误切换预测扣分

# ── 权重建议幅度 ──
WEIGHT_DELTA = 0.05            # 每次建议加减幅度

DEFAULT_HISTORY_DAYS = 30


# ═══════════════════════════════════════════
# Entry: 加载历史数据
# ═══════════════════════════════════════════

def _load_candidates_from_replay(days: int = 5) -> list[dict]:
    """从 replay 加载近 N 天的所有候选股记录

    每一条记录为：
      {
        "date": "20260526",
        "snapshot_id": "20260526-01",
        "regime": "rotational_chop",
        "primary_strategy": "sector_rotation",
        "candidates": [{"symbol":..., "score":..., "tier":..., ...}]
      }
    """
    from replay.recorder import list_snapshots

    records = []
    today = datetime.now()
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        snaps = list_snapshots(d, limit=100)
        for s in snaps:
            records.append({
                "date": s.get("date", d),
                "snapshot_id": s.get("snapshot_id", ""),
                "regime": s.get("regime", ""),
                "primary_strategy": s.get("primary_strategy", ""),
                "active_strategies": s.get("active_strategies", []),
                "rotation_speed": s.get("rotation_speed", 0.0),
                "candidates": s.get("candidates", []),
                "events": s.get("events", []),
            })
    return records


def _load_validation_results() -> list[dict]:
    """从 replay/validation/ 目录加载验证结果"""
    val_dir = os.path.join(
        os.path.dirname(__file__), "..", "data", "replay", "validation"
    )
    if not os.path.isdir(val_dir):
        return []
    results = []
    for fname in sorted(os.listdir(val_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(val_dir, fname)
        with open(path) as f:
            results.append(json.load(f))
    return results


# ═══════════════════════════════════════════
# 1. Tier Accuracy
# ═══════════════════════════════════════════

def tier_accuracy(
    replay_records: list[dict],
    validation_results: list[dict],
) -> dict:
    """计算每级候选股的实际表现准确率

    指标：
      - win_rate:     S/A/B/Watchlist 各自上涨比例
      - beat_sector:  跑赢板块比例
      - avg_pct:      S/A/B/Watchlist 平均涨幅
      - dist:         S/A/B/Watchlist 今日数量分布
      - aligned:      S 胜率是否 ≥ A ≥ B ≥ Watchlist（排序一致性检查）
      - warnings:     tier_accuracy 异常告警
    """
    # 从验证结果中聚合
    tier_stats = defaultdict(lambda: {
        "total": 0, "wins": 0, "beat_sector": 0, "total_pct": 0.0,
    })

    for v in validation_results:
        cand = v.get("candidates", {})
        by_tier = cand.get("by_tier", {})
        for tier, stats in by_tier.items():
            ts = tier_stats[tier]
            ts["total"] += stats.get("total", 0)
            ts["wins"] += stats.get("win", 0)
            ts["total_pct"] += stats.get("avg_pct", 0) * stats.get("total", 0)

    # 计算总体指标
    tier_report = {}
    for tier in ("S", "A", "B", "Watchlist"):
        ts = tier_stats[tier]
        if ts["total"] == 0:
            continue
        win_rate = round(ts["wins"] / ts["total"], 3)
        avg_pct = round(ts["total_pct"] / ts["total"], 2)
        threshold = TIER_WIN_THRESHOLDS.get(tier, 0.3)

        warning = None
        if win_rate < threshold:
            warning = (
                f"{tier} win_rate={win_rate:.1%} < threshold={threshold:.0%} → "
                f"可能分级偏松或权重需调整"
            )

        tier_report[tier] = {
            "total": ts["total"],
            "wins": ts["wins"],
            "win_rate": win_rate,
            "avg_pct": avg_pct,
            "threshold": threshold,
            "aligned": win_rate >= threshold,
            "warning": warning,
        }

    # 排序一致性检查
    ordered = [t for t in ("S", "A", "B", "Watchlist") if t in tier_report]
    aligned = all(
        tier_report[ordered[i]]["win_rate"] >= tier_report[ordered[i + 1]]["win_rate"]
        for i in range(len(ordered) - 1)
        if tier_report[ordered[i]]["total"] > 0 and tier_report[ordered[i + 1]]["total"] > 0
    )

    return {
        "tiers": tier_report,
        "sorted_by_winrate": aligned,
        "warnings": [v["warning"] for v in tier_report.values() if v.get("warning")],
    }


# ═══════════════════════════════════════════
# 2. Strategy Winrate
# ═══════════════════════════════════════════

def strategy_winrate(
    replay_records: list[dict],
    validation_results: list[dict],
) -> dict:
    """计算各策略候选股胜率

    如果有次日验证数据，使用验证数据。
    如果没有，使用 replay 中的候选股 day-of 数据做一个"日间预估"。

    输出：
      {
        "sector_rotation": {"total": 20, "wins": 12, "win_rate": 0.6, "avg_pct": 1.2, "needs_tune": False},
        ...
        "warnings": [...]
      }
    """
    # 从验证结果中聚合策略胜率
    strategy_stats = defaultdict(lambda: {"total": 0, "wins": 0, "total_pct": 0.0})

    # 从 replay records 中聚合策略候选分布
    strategy_count = defaultdict(int)
    strategy_scores = defaultdict(list)

    for rec in replay_records:
        pst = rec.get("primary_strategy", "")
        strategy_count[pst] += len(rec.get("candidates", []))
        for c in rec.get("candidates", []):
            strategy_scores[pst].append(c.get("score", 0))

    # 如果有 validation_results，逐日对应
    for v in validation_results:
        # 找到对应日期的 replay
        v_date = v.get("date", "")
        for rec in replay_records:
            if rec.get("date") == v_date:
                pst = rec.get("primary_strategy", "")
                # 候选股赢数
                cand_report = v.get("candidates", {})
                if cand_report.get("total", 0) < 1:
                    continue
                strategy_stats[pst]["total"] += cand_report["total"]
                strategy_stats[pst]["wins"] += cand_report.get("profitable", 0)
                strategy_stats[pst]["total_pct"] += cand_report.get("avg_score", 0) or 0
                break

    report = {}
    warnings = []
    for strategy in sorted(set(list(strategy_stats.keys()) + list(strategy_count.keys()))):
        ss = strategy_stats.get(strategy, {})
        total = ss.get("total", 0)
        if total == 0:
            # 没有验证数据，使用候选数量 + 平均分做占位
            cnt = strategy_count.get(strategy, 0)
            avg_score = 0.0
            if cnt > 0 and strategy in strategy_scores:
                avg_score = round(sum(strategy_scores[strategy]) / cnt, 1)
            report[strategy] = {
                "total": cnt,
                "wins": 0,
                "win_rate": 0.0,
                "avg_pct": 0.0,
                "avg_score": avg_score,
                "candidates_count": cnt,
                "needs_tune": False,
                "note": "no validation data yet",
            }
            continue

        wins = ss.get("wins", 0)
        win_rate = round(wins / total, 3) if total > 0 else 0.0
        avg_pct = round(ss.get("total_pct", 0) / total, 2) if total > 0 else 0.0
        needs_tune = win_rate < STRATEGY_WIN_THRESHOLD

        if needs_tune:
            warnings.append(
                f"{strategy} win_rate={win_rate:.1%} < threshold={STRATEGY_WIN_THRESHOLD:.0%} → "
                f"需调参"
            )

        report[strategy] = {
            "total": total,
            "wins": wins,
            "win_rate": win_rate,
            "avg_pct": avg_pct,
            "needs_tune": needs_tune,
        }

    return {
        "strategies": report,
        "warnings": warnings,
    }


# ═══════════════════════════════════════════
# 3. Regime Validation
# ═══════════════════════════════════════════

def regime_validation(
    replay_records: list[dict],
) -> dict:
    """验证市场状态识别的稳定性

    指标：
      - regime_churn:     N 日内 regime 切换频率（过高 = 不稳定）
      - rotation_stable:  rotation_speed 标准差（小 = 稳定）
      - consecutive:      同一 regime 持续记录数
      - risk_stable:      risk_level 标准差
      - warnings:         regime 质量异常
    """
    if not replay_records:
        return {"regime_churn": 0, "rotation_stable": 0, "consecutive": 0, "warnings": ["no data"]}

    sorted_records = sorted(replay_records, key=lambda r: r.get("snapshot_id", ""))

    regimes = [r.get("regime", "") for r in sorted_records]
    rotations = [r.get("rotation_speed", 0.5) for r in sorted_records]
    risks = [r.get("risk_level", 0.5) for r in sorted_records]

    # regime 切换
    switches = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i - 1])
    churn = round(switches / max(len(regimes), 1), 3)

    # rotation 稳定性
    n = len(rotations)
    if n > 1:
        mean_r = sum(rotations) / n
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in rotations) / n)
    else:
        std_r = 0.0

    # risk 稳定性
    if n > 1:
        mean_risk = sum(risks) / n
        std_risk = math.sqrt(sum((r - mean_risk) ** 2 for r in risks) / n)
    else:
        std_risk = 0.0

    # 当前 regime 持续次数
    if regimes:
        current = regimes[-1]
        consecutive = 0
        for r in reversed(regimes):
            if r == current:
                consecutive += 1
            else:
                break
    else:
        consecutive = 0

    warnings = []
    if churn > 0.3:
        warnings.append(f"regime churn={churn:.2f} (high: >0.3) → regime 切换过频")
    if std_r > 0.2 and n > 3:
        warnings.append(f"rotation std={std_r:.2f} (high: >0.2) → 轮动不稳定")
    if std_risk > 0.15 and n > 3:
        warnings.append(f"risk std={std_risk:.2f} (high: >0.15) → 风控波动大")

    return {
        "total_records": n,
        "regime_churn": round(churn, 3),
        "rotation_std": round(std_r, 3),
        "risk_std": round(std_risk, 3),
        "consecutive_same_regime": consecutive,
        "current_regime": regimes[-1] if regimes else "",
        "regime_switches": switches,
        "warnings": warnings,
    }


# ═══════════════════════════════════════════
# 4. Weight Rebalance Suggestions
# ═══════════════════════════════════════════

def weight_rebalance(
    tier_report: dict,
    strategy_report: dict,
    regime_report: dict,
) -> dict:
    """根据校准结果输出 Ranking V2 权重调整建议

    输出内容：
      - tier_threshold_adjust:  S/A/B 分级阈值建议
      - strategy_weight_adjust: 各策略的权重建议
      - component_adjust:       7因子×调整建议
      - penalty_adjust:         惩罚项调整建议
      - priority:               建议修改优先级
    """
    suggestions = []
    priority = []

    # ── 分级阈值调整 ──
    tier_adjust = {}
    tiers = tier_report.get("tiers", {})
    for t in ("S", "A", "B"):
        info = tiers.get(t, {})
        if not info:
            continue
        wr = info.get("win_rate", 0)
        threshold = TIER_WIN_THRESHOLDS.get(t, 0.3)

        if wr < threshold * 0.8:
            # 严重低于阈值：提高分级门槛
            tier_adjust[t] = {
                "action": "raise_threshold",
                "reason": f"win_rate={wr:.1%} << threshold={threshold:.0%}",
                "suggested": f"raise {t} score_threshold by {WEIGHT_DELTA:.0%}",
            }
            priority.append(f"[高] Tier {t} 阈值需提高")
            suggestions.append(f"raise {t} score threshold")

        elif wr < threshold:
            # 轻微低于阈值：观察
            tier_adjust[t] = {
                "action": "monitor",
                "reason": f"win_rate={wr:.1%} < threshold={threshold:.0%}",
            }

    # ── 策略权重调整 ──
    strategy_adjust = {}
    for s, info in strategy_report.get("strategies", {}).items():
        if not info.get("total", 0) > 0:
            continue
        if info.get("needs_tune"):
            adj = WEIGHT_DELTA * -1  # 降低权重
            strategy_adjust[s] = {
                "action": f"decrease_score_offset by {abs(adj):.0%}",
                "reason": f"win_rate={info.get('win_rate', 0):.1%} < threshold",
            }
            priority.append(f"[中] Strategy {s} 权重需降低")
        elif info.get("win_rate", 0) > 0.6:
            adj = WEIGHT_DELTA
            strategy_adjust[s] = {
                "action": f"increase_score_offset by {adj:.0%}",
                "reason": f"win_rate={info.get('win_rate', 0):.1%} > 60% (strong)",
            }

    # ── 组件权重调整 ──
    component_adjust = {}
    regime_regime = regime_report.get("current_regime", "")
    if regime_regime == "rotational_chop" and regime_report.get("rotation_std", 0) > 0.15:
        component_adjust["momentum_quality"] = {
            "note": "rotation unstable → reduce momentum_weight or make regime-aware",
        }
    if regime_report.get("regime_churn", 0) > 0.2:
        component_adjust["strategy_fit"] = {
            "note": "frequent regime switch → de-penalize strategy_fit mismatch",
        }

    # ── 惩罚项调整 ──
    penalty_adjust = {}
    if tier_report.get("sorted_by_winrate") is False:
        penalty_adjust["exhaustion_penalty"] = {
            "action": "increase",
            "reason": "tier not monotonic → need stronger high-score discrimination",
        }

    return {
        "tier_threshold_adjust": tier_adjust,
        "strategy_weight_adjust": strategy_adjust,
        "component_adjust": component_adjust,
        "penalty_adjust": penalty_adjust,
        "priority": priority,
        "suggestions": suggestions,
    }


# ═══════════════════════════════════════════
# 5. Calibration Report — 唯一入口
# ═══════════════════════════════════════════

def calibration_report(days: int = DEFAULT_HISTORY_DAYS) -> dict:
    """生成完整校准报告

    1. 加载历史 replay 数据和验证数据
    2. 计算 tier_accuracy
    3. 计算 strategy_winrate
    4. 计算 regime_validation
    5. 输出 weight_rebalance 建议
    6. 汇总评分

    返回：
      {
        "ts": ISO,
        "data_range": "N days",
        "replay_count": int,
        "validation_count": int,
        "tier_accuracy": { ... },
        "strategy_winrate": { ... },
        "regime_validation": { ... },
        "weight_rebalance": { ... },
        "calibration_score": float,    # 0-100，系统校准质量
        "warnings": [str, ...],
      }
    """
    replay_records = _load_candidates_from_replay(days=days)
    validation_results = _load_validation_results()

    t_accuracy = tier_accuracy(replay_records, validation_results)
    s_winrate = strategy_winrate(replay_records, validation_results)
    r_validation = regime_validation(replay_records)
    w_rebalance = weight_rebalance(t_accuracy, s_winrate, r_validation)

    # ── 校准总分 ──
    score = 50.0

    # tier 排序一致性
    if t_accuracy.get("sorted_by_winrate"):
        score += 20

    # regime 稳定性
    rc = r_validation.get("regime_churn", 1)
    if rc < 0.1:
        score += 15
    elif rc < 0.2:
        score += 10

    # 策略胜率
    strats = s_winrate.get("strategies", {})
    if strats:
        above_threshold = sum(
            1 for s in strats.values()
            if s.get("total", 0) > 0 and s.get("win_rate", 0) >= STRATEGY_WIN_THRESHOLD
        )
        total_active = max(sum(1 for s in strats.values() if s.get("total", 0) > 0), 1)
        score += 15 * (above_threshold / total_active)

    score = round(min(100.0, max(0.0, score)), 1)

    # ── 全部警告 ──
    all_warnings = []
    all_warnings.extend(t_accuracy.get("warnings", []))
    all_warnings.extend(s_winrate.get("warnings", []))
    all_warnings.extend(r_validation.get("warnings", []))

    return {
        "ts": datetime.now().isoformat(),
        "data_range": f"{days} days",
        "replay_count": len(replay_records),
        "validation_count": len(validation_results),
        "tier_accuracy": t_accuracy,
        "strategy_winrate": s_winrate,
        "regime_validation": r_validation,
        "weight_rebalance": w_rebalance,
        "calibration_score": score,
        "warnings": all_warnings,
    }
