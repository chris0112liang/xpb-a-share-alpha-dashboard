"""
sector_momentum.py — 板块动量排名 + 轮动速度

依赖于 sector_lifecycle_6stage.py 的计算结果。
独立性：仅读取全局缓存，不做 I/O。

输出：
  1. momentum_ranking — 全板块按综合动量从强到弱排序
  2. rotation_speed  — 市场轮动速度（板块间动量模式变化速率）
  3. sector_heatmap  — 各板块 + 各 phase 聚合视图
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from typing import Optional

import numpy as np


def compute_momentum_ranking(
    lifecycles: dict[str, dict],
) -> list[dict]:
    """
    全板块动量排序

    输入：SECTOR_LIFECYCLE_FULL（各板块 6 阶段结果）
    输出：按 momentum_score 从高到低排序的列表
    """
    ranking = []

    for sector_name, lc in lifecycles.items():
        if not isinstance(lc, dict):
            continue

        phase = lc.get("phase", "unknown")
        bias = lc.get("bias", 0)
        strength = lc.get("strength_score", 0)
        confidence = lc.get("confidence", 0)
        accel = lc.get("acceleration", 0)
        rs_slope = lc.get("rs_slope", 0)
        vol_trend = lc.get("vol_trend", 0)
        price_mom_5 = lc.get("price_mom_5", 0)
        days_active = lc.get("days_active", 0)
        is_turning = lc.get("is_turning", False)

        # 综合动量分 [0, 100]
        momentum = _compute_momentum_score(
            phase, strength, bias, accel, rs_slope, vol_trend, price_mom_5, confidence, is_turning,
        )

        ranking.append({
            "name": sector_name,
            "momentum_score": round(momentum, 1),
            "phase": phase,
            "phase_seq": lc.get("phase_seq", -1),
            "bias": lc.get("bias", 0),
            "strength_score": strength,
            "confidence": confidence,
            "acceleration": lc.get("acceleration", 0),
            "rs_slope": lc.get("rs_slope", 0),
            "vol_trend": lc.get("vol_trend", 0),
            "price_mom_5": price_mom_5,
            "days_active": days_active,
            "is_turning": is_turning,
        })

    # 按动量分排序
    ranking.sort(key=lambda x: -x["momentum_score"])
    return ranking


def _compute_momentum_score(
    phase: str, strength: float, bias: float, accel: float,
    rs_slope: float, vol_trend: float, price_mom_5: float,
    confidence: float, is_turning: bool,
) -> float:
    """综合动量分 [0, 100]"""

    # ── 阶段系数 ──
    phase_base = {
        "acceleration": 90,
        "main_rise_1": 75,
        "startup": 55,
        "high_divergence": 40,
        "ice_recovery": 30,
        "decay": 15,
        "unknown": 5,
    }.get(phase, 10)

    # ── 强度分加权 ──
    strength_factor = strength * 0.3

    # ── 乖离加权（过大 = 过热需警惕） ──
    bias_score = min(15, max(-5, abs(bias) * 0.8))
    if abs(bias) > 10:
        bias_score -= 5  # 乖离过大 → 回调风险

    # ── 加速度加成（正加速 → 加分，减速 → 扣分） ──
    accel_score = min(10, max(-10, accel * 1.5))

    # ── RS 趋势 ──
    rs_score = min(10, max(-5, rs_slope * 2))

    # ── 成交量确认（量能同步 → 健康） ──
    vol_score = 0
    if vol_trend > 10 and abs(bias) > 2:
        vol_score = 8  # 量能紧跟 → 健康
    elif vol_trend < -15 and abs(bias) > 2:
        vol_score = -5  # 价涨量缩 → 危险

    # ── 动量趋势一致性 ──
    consistency_bonus = 0
    if phase in ("main_rise_1", "acceleration") and price_mom_5 > 3:
        consistency_bonus = 10
    elif phase == "startup" and price_mom_5 > 0:
        consistency_bonus = 5

    # ── 拐点惩罚 ──
    turning_penalty = -15 if is_turning else 0

    # ── 置信度调节 ──
    raw = (phase_base + strength_factor + bias_score + accel_score +
           rs_score + vol_score + consistency_bonus + turning_penalty)
    raw = raw * (0.5 + confidence * 0.5)

    return max(0, min(100, raw))


def compute_rotation_speed(
    current_ranking: list[dict],
    prev_ranking: Optional[list[dict]] = None,
) -> float:
    """
    板块轮动速度 [0, 1]

    0.0 = 完全静止（主线高度集中，无轮动）
    0.5 = 正常轮动
    1.0 = 极快轮动（电风扇行情）

    算法：前 10 强板块的动量变异系数 + 阶段分布熵
    """
    if not current_ranking:
        return 0.5

    top10 = [r for r in current_ranking if r["momentum_score"] > 0][:10]
    if len(top10) < 3:
        return 0.5

    # 1. 动量分散度：CV（变异系数）越大 → 轮动越快
    scores = np.array([r["momentum_score"] for r in top10])
    cv = np.std(scores) / (np.mean(scores) + 1e-8)

    # 2. 阶段多样性：各阶段分布越均匀 → 轮动越快
    phases = [r["phase"] for r in current_ranking]
    phase_counts = Counter(phases)
    total = len(phases)
    entropy = -sum(
        (c / total) * math.log(c / total) for c in phase_counts.values()
    )
    max_entropy = math.log(6)  # 6 阶段
    norm_entropy = entropy / max_entropy if max_entropy > 0 else 0

    # 3. 有无明确主线：top 3 集中度
    top3_score = sum(r["momentum_score"] for r in top10[:3]) / \
                 (sum(r["momentum_score"] for r in top10) + 1e-8)

    # 浓聚度因子：top3 占比高 → 轮动慢
    concentration = max(0, min(1, (top3_score - 0.3) / 0.5))

    # 综合轮动速度
    rotation = cv * 0.25 + norm_entropy * 0.4 + (1 - concentration) * 0.35
    return round(float(min(1.0, max(0.0, rotation))), 4)


def compute_sector_heatmap(
    lifecycles: dict[str, dict],
) -> dict:
    """
    板块热力图数据

    输出：
    {
      "phase_distribution": {"acceleration": 5, "main_rise_1": 8, ...},
      "top_momentum": [...],      # 前 15 板块
      "bottom_momentum": [...],   # 后 5 板块
      "top_turning": [...],       # 处于拐点的板块
      "leading_sectors": [...],   # 领涨板块
    }
    """
    ranking = compute_momentum_ranking(lifecycles)
    phase_dist = Counter(
        lc.get("phase", "unknown")
        for lc in lifecycles.values() if isinstance(lc, dict)
    )

    # 领涨板块：阶段为 acceleration + main_rise_1，按动量排序
    leading = [
        r for r in ranking
        if r["phase"] in ("acceleration", "main_rise_1")
    ][:5]

    # 拐点预警
    turning = [
        {"name": r["name"], "phase": r["phase"], "momentum_score": r["momentum_score"]}
        for r in ranking if r["is_turning"]
    ]

    return {
        "phase_distribution": dict(phase_dist),
        "total_sectors": len(lifecycles),
        "top_momentum": ranking[:15],
        "top_5": ranking[:5],
        "bottom_5": ranking[-5:] if len(ranking) >= 5 else ranking,
        "turning_sectors": turning,
        "leading_sectors": leading,
    }


def get_leading_sectors(
    ranking: list[dict], top_n: int = 3,
) -> list[str]:
    """获取领涨板块名称列表"""
    return [
        r["name"] for r in ranking[:top_n]
        if r["phase"] in ("acceleration", "main_rise_1", "startup")
    ][:top_n]
