"""
alpha/filters.py — 策略驱动扫描过滤器

每个策略有对应的 filter 函数：
  filter_trend_breakout(stock, ctx) → (score, reasons)
  filter_sector_rotation(stock, ctx) → (score, reasons)
  filter_dip_stabilization(stock, ctx) → (score, reasons)
  filter_oversold_reversal(stock, ctx) → (score, reasons)

ctx = {
  "sector_phase": str,          # 板块生命周期阶段
  "sector_momentum_rank": int,  # 板块动量排名
  "sector_bias": float,         # 板块乖离率
  "is_leading": bool,           # 是否领涨板块成员
  "index_change_pct": float,    # 大盘当日涨跌
}
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def filter_trend_breakout(stock: dict, ctx: dict) -> tuple[float, list[str]]:
    """
    趋势突破扫描：
    - 板块处于 main_rise_1 / acceleration
    - 个股强于板块（change_pct > sector avg）
    - 放量（volume_ratio > 1.3）
    - 成交额 TOP 500
    - 高换手（活跃度）
    """
    score = 0.0
    reasons = []

    change_pct = stock.get("change_pct", 0)
    amount = stock.get("amount", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    turnover = stock.get("turnover", 0)
    rank = stock.get("amount_rank", 9999)

    sector_phase = ctx.get("sector_phase", "")
    is_leading = ctx.get("is_leading", False)

    # 板块阶段——核心条件
    if sector_phase in ("main_rise_1", "acceleration"):
        score += 30
        reasons.append(f"板块处于{sector_phase}期")

    # 领涨板块
    if is_leading:
        score += 15
        reasons.append("所属板块为当前领涨板块")

    # 放量
    if volume_ratio > 1.3:
        bonus = min(15, (volume_ratio - 1.3) * 10)
        score += bonus
        reasons.append(f"放量{volume_ratio:.2f}倍")

    # 成交活跃
    if rank < 500:
        score += 10
        reasons.append(f"成交额排名TOP{rank}")

    if turnover > 8:
        score += 5
        reasons.append("换手率>8%")

    # 涨幅适中（太极端不好）
    if 3 < change_pct < 10:
        score += 10
        reasons.append(f"涨幅{change_pct:+.1f}%")

    # 强于板块
    stock_vs_sector = stock.get("stronger_than_sector", False)
    if stock_vs_sector:
        score += 10
        reasons.append("强于所属板块")

    score = min(score, 100)
    return score, reasons


def filter_sector_rotation(stock: dict, ctx: dict) -> tuple[float, list[str]]:
    """
    板块轮动扫描：
    - 板块是 leading_sector
    - 板块阶段非 acceleration（避免高位接）
    - 个股缩量回调企稳
    - 首次分歧回踩
    - 换手适中
    """
    score = 0.0
    reasons = []

    change_pct = stock.get("change_pct", 0)
    change_pct_5d = stock.get("change_pct_5d", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    turnover = stock.get("turnover", 0)

    sector_phase = ctx.get("sector_phase", "")
    is_leading = ctx.get("is_leading", False)
    is_first_pullback = stock.get("is_first_pullback", False)
    is_volume_shrink = stock.get("is_volume_shrink", False)

    # 领涨板块
    if is_leading:
        score += 20
        reasons.append("板块在领涨行列")

    # 避免加速高位接
    if sector_phase in ("startup", "main_rise_1", "high_divergence"):
        score += 15
        reasons.append(f"板块处于{sector_phase}期（非加速高位）")

    # 首次分歧回踩——核心买点
    if is_first_pullback:
        score += 25
        reasons.append("首次分歧回踩，核心低吸买点")

    # 缩量企稳（非放量下跌）
    if is_volume_shrink:
        score += 10
        reasons.append("缩量企稳")

    # 调整幅度适中（-2%~-8% 之间是好的低吸区间）
    if -8 < change_pct < -0.5:
        bonus = min(10, abs(change_pct))
        score += bonus
        reasons.append(f"当日调整{change_pct:+.1f}%，适合低吸")

    # 5日回调但未破趋势
    if -15 < change_pct_5d < -2:
        score += 8
        reasons.append(f"5日回调{change_pct_5d:+.1f}%，非崩塌")

    # 换手不太低（有流动性）
    if 1 < turnover < 15:
        score += 5
    elif turnover >= 15:
        score -= 5  # 换手太高可能是出货
        reasons.append("换手率偏高注意风险")

    score = max(0, min(score, 100))
    return score, reasons


def filter_dip_stabilization(stock: dict, ctx: dict) -> tuple[float, list[str]]:
    """
    分歧低吸扫描：
    - 个股属于拐点预警板块（turning）
    - 板块处于 acceleration → 转分歧
    - 个股缩量
    - 个股在关键支撑附近
    - 逻辑类似 sector_rotation 但有差异
    """
    score = 0.0
    reasons = []

    change_pct_5d = stock.get("change_pct_5d", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    change_pct = stock.get("change_pct", 0)
    sector_phase = ctx.get("sector_phase", "")

    # 板块分歧
    if sector_phase == "high_divergence":
        score += 20
        reasons.append("板块处于高位分歧期")

    # 个股回调幅度（分歧低吸找跌的）
    if -5 > change_pct > -10:
        score += 15
        reasons.append(f"个股回调{change_pct:+.1f}%，低吸窗口")

    # 个股缩量（非恐慌）
    if volume_ratio < 0.8:
        score += 10
        reasons.append("缩量回调，非恐慌抛售")

    # 5日跌不少但在修复
    if -12 < change_pct_5d < -3:
        score += 8
        reasons.append(f"5日回调{change_pct_5d:+.1f}%，有修复空间")

    # 流动性（能承接）
    rank = stock.get("amount_rank", 9999)
    if rank < 800:
        score += 5
        reasons.append("流动性适中")

    # 当前日阳线（企稳信号）
    if change_pct > 0 and sector_phase == "high_divergence":
        score += 12
        reasons.append("分歧中收阳，企稳信号")

    score = min(score, 100)
    return score, reasons


def filter_oversold_reversal(stock: dict, ctx: dict) -> tuple[float, list[str]]:
    """
    超跌反弹扫描：
    - 板块处于 ice_recovery
    - 个股大跌幅后放量反包
    - 5日重挫后止跌
    """
    score = 0.0
    reasons = []

    change_pct = stock.get("change_pct", 0)
    change_pct_5d = stock.get("change_pct_5d", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    sector_phase = ctx.get("sector_phase", "")

    # 板块冰点修复
    if sector_phase == "ice_recovery":
        score += 25
        reasons.append("板块处于冰点修复阶段")

    # 5日重挫（超跌条件）
    if change_pct_5d < -10:
        bonus = min(15, abs(change_pct_5d) * 0.5)
        score += bonus
        reasons.append(f"5日跌幅{change_pct_5d:+.1f}%，超跌")

    # 当日放量反弹
    if change_pct > 2 and volume_ratio > 1.2:
        score += 15
        reasons.append("放量反弹")
    elif change_pct > 4:
        score += 10
        reasons.append("大阳线反弹")

    # 底部放量反包
    if stock.get("is_volume_expand", False) and change_pct > 0:
        score += 10
        reasons.append("底部放量")

    # 跌停减少（无法精确检查，用成交额作流替代）
    rank = stock.get("amount_rank", 9999)
    if rank < 1000:
        score += 5

    score = min(score, 100)
    return score, reasons
