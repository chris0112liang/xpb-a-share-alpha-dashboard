"""
sector_lifecycle_6stage.py — 板块生命周期 6 阶段引擎

取代旧版 3/4 阶段投票，升级为：

  启动期 → 主升一期 → 加速期 → 高位分歧 → 退潮期 → 冰点修复

每条路径意味着：
  启动期：资金试探性入场，量能温和放大
  主升一期：量价齐升，板块共识形成
  加速期：情绪放大，量价极端，趋势加速
  高位分歧：控盘力量减弱，量价背离
  退潮期：资金系统性退出
  冰点修复：调整到极致，空头衰竭，开始有资金回流

设计原则：
  1. 不是简单阈值打分，而是"市场行为模式匹配"
  2. 每个阶段有明确的量价行为签名
  3. 使用 20/60 日双窗口判断趋势稳定性
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import numpy as np

# ── 阶段定义 ──

PHASE_ORDER = [
    "ice_recovery",    # 冰点修复
    "startup",         # 启动期
    "main_rise_1",     # 主升一期
    "acceleration",    # 加速期
    "high_divergence", # 高位分歧
    "decay",           # 退潮期
]

PHASE_CN = {
    "ice_recovery": "冰点修复",
    "startup": "启动期",
    "main_rise_1": "主升一期",
    "acceleration": "加速期",
    "high_divergence": "高位分歧",
    "decay": "退潮期",
    "unknown": "感知中断",
    "detect_failed": "数据缺失",
}


# ═══════════════════════════════════════════════════════════
# 铁血防御性安全除法 —— 零值/None 绝不抛异常，老老实实输出 0.0
# ═══════════════════════════════════════════════════════════

def safe_bias(close: float, ma: float) -> float:
    """乖离率：(close - ma) / ma * 100。零分母/None → 0.0"""
    if ma is None or close is None or ma == 0 or close == 0:
        return 0.0
    return float((close - ma) / ma * 100)


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """安全除法 a/b。零分母/None → default"""
    if b is None or a is None or b == 0:
        return float(default)
    return float(a / b)


def safe_momentum(current: float, previous: float) -> float:
    """动量：(current / previous - 1) * 100。零分母/None → 0.0"""
    if previous is None or current is None or previous == 0:
        return 0.0
    return float((current / previous - 1) * 100)


def safe_ratio(a: float, b: float, default: float = 1.0) -> float:
    """比值 a/b。零分母/None → default（默认 1.0 表示无变化）"""
    if b is None or a is None or b == 0:
        return float(default)
    return float(a / b)


def _default_result(phase: str, confidence: float) -> dict:
    """数据不足时的默认结果"""
    return {
        "phase": phase,
        "confidence": confidence,
        "phase_seq": PHASE_ORDER.index(phase) if phase in PHASE_ORDER else -1,
        "scores": {},
        "bias": 0.0,
        "price_mom_5": 0.0,
        "price_mom_20": 0.0,
        "vol_mom_5": 0.0,
        "vol_ratio": 1.0,
        "acceleration": 0.0,
        "rs_slope": 0.0,
        "vol_trend": 0.0,
        "days_active": 0,
        "consistency": 0.0,
        "strength_score": 0.0,
        "is_turning": False,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _estimate_days_in_phase(
    close_vals: np.ndarray, amount_vals: np.ndarray,
) -> int:
    """估计当前阶段持续天数"""
    n = len(close_vals)
    if n < 5:
        return 0
    ma5 = np.array([np.mean(close_vals[max(0, i - 4):i + 1]) for i in range(n)])
    ma20 = np.mean(close_vals)
    cross_days = 0
    for i in range(n - 1, 0, -1):
        if abs(safe_bias(close_vals[i], ma20)) < 2:
            cross_days = n - 1 - i
            break
    return min(cross_days, n)


def _compute_strength_score(
    phase: str, bias: float, price_5: float, price_20: float,
    vol_ratio: float, confidence: float,
) -> float:
    """综合强度分 [0, 100]"""
    phase_base = {
        "ice_recovery": 20, "startup": 35,
        "main_rise_1": 55, "acceleration": 75,
        "high_divergence": 50, "decay": 15, "unknown": 5,
        "detect_failed": 0,
    }.get(phase, 5)
    mom = min(30, max(-10, price_5 * 1.2 + price_20 * 0.5))
    vol = min(15, max(-5, (vol_ratio - 1) * 40))
    conf = 0.5 + confidence * 0.5
    return max(0, min(100, (phase_base + mom + vol) * conf))


def _validate_data_completeness(
    close_vals: np.ndarray, amount_vals: np.ndarray,
) -> str | None:
    """
    数据完备性校验 —— 在任何阶段判定之前执行。

    返回 None = 数据正常，可继续分类。
    返回 "detect_failed" = 完全无数据（网络全断/全零回填），前端红色"感知中断"。
    返回 "unknown" = 数据存在但不足以分类，前端灰色"数据不足"。

    ★ 策略：只有真正全零才判 detect_failed，数据稀疏判 unknown 而非直接拒绝。
    """
    n = len(close_vals)
    if n < 5:  # 连 5 根 K 线都没有 → 硬失败
        return "detect_failed"

    # 完全零回填 → 网络中断
    if np.all(close_vals == 0) or np.max(close_vals) <= 0:
        return "detect_failed"

    # 数据量不足 20 → 可以计算但不可靠，仍让引擎跑
    if n < 20:
        return None  # 让它跑，引擎内部会处理小样本

    # 最近 20 日超 70% 为零 → 数据严重缺失
    recent = close_vals[-min(20, n):]
    zero_ratio = np.sum(recent == 0) / len(recent)
    if zero_ratio > 0.7:
        return "detect_failed"

    # 非零价格完全无波动 → 可能数据异常
    non_zero = close_vals[close_vals > 0]
    if len(non_zero) >= 5 and np.std(non_zero) == 0:
        return "detect_failed"

    return None  # 数据有值，继续分类


def compute_6stage_lifecycle(
    close_vals: np.ndarray,
    amount_vals: np.ndarray,
) -> dict:
    """
    核心：6 阶段生命周期计算

    输入：
      close_vals  — 按时间升序的日收盘价 (numpy, n>=20)
      amount_vals — 按时间升序的日成交额 (numpy, same length)

    输出：
      {
        "phase":  "acceleration" | "startup" | "main_rise_1" | ... | "unknown" | "detect_failed",
        "confidence": 0.85,
        "phase_seq": int,                # 0-5, -1 for unknown/outside order
        ...
      }

    诚实分类原则：
      - 当明确符合某个阶段签名时 → 归入该阶段 (Softmax)
      - 当证据不足 (total_raw < 6) 时 → 返回 "unknown"
      - ★ 数据完备性校验失败 → 返回 "detect_failed"（网络中断/数据缺失）
      - "unknown" = 「数据不足以归入任何阶段」，非"无趋势"
      - "detect_failed" = 「数据源不可用」，前端显示"感知中断"
    """
    n = len(close_vals)
    if n < 20:
        return _default_result("detect_failed", 0.0)

    # ═══════════════════════════════════════════
    # ★ 铁血数据完备性校验 —— 先于任何阶段判定
    # ═══════════════════════════════════════════
    fail_reason = _validate_data_completeness(close_vals, amount_vals)
    if fail_reason is not None:
        return _default_result(fail_reason, 0.0)

    # ═══════════════════════════════════
    # 1. 原始指标计算
    # ═══════════════════════════════════

    # 乖离率：从 MA20 偏离程度
    ma20 = np.mean(close_vals[-20:])
    bias = safe_bias(close_vals[-1], ma20)

    # MA 系统
    ma5 = np.mean(close_vals[-5:]) if n >= 5 else close_vals[-1]
    ma10 = np.mean(close_vals[-10:]) if n >= 10 else close_vals[-1]

    # 价格动量（多窗口）—— 全部走 safe_momentum
    price_3 = safe_momentum(close_vals[-1], close_vals[-3]) if n >= 3 else 0.0
    price_5 = safe_momentum(close_vals[-1], close_vals[-5]) if n >= 5 else 0.0
    price_10 = safe_momentum(close_vals[-1], close_vals[-10]) if n >= 10 else 0.0
    price_20 = safe_momentum(close_vals[-1], close_vals[-20]) if n >= 20 else 0.0

    # 短期相对于中期的偏离（MA5 vs MA10）
    ma5_over_ma10 = safe_momentum(ma5, ma10)

    # 成交量分析
    vol_5 = np.mean(amount_vals[-5:]) if n >= 5 else amount_vals[-1]
    vol_10 = np.mean(amount_vals[-10:]) if n >= 10 else amount_vals[-1]
    vol_20 = np.mean(amount_vals[-20:]) if n >= 20 else amount_vals[-1]

    # 量比
    vol_ratio_5_20 = safe_ratio(vol_5, vol_20, 1.0)
    vol_mom_5 = safe_momentum(vol_5, vol_10)

    # 加速度 = 近5日动量 - 前5日动量（是否在加速？）
    if n >= 11:
        num, den = close_vals[-6:-1], close_vals[-11:-6]
    elif n >= 6:
        num, den = close_vals[-5:], close_vals[-10:-5]
    else:
        num, den = np.array([]), np.array([])

    if len(num) > 0 and len(den) > 0:
        m = min(len(num), len(den))
        price_prior_5 = np.array([safe_div(num[i], den[i], 1.0) - 1.0 for i in range(m)])
        price_prior_5_mean = float(np.mean(price_prior_5) * 100)
    else:
        price_prior_5_mean = 0.0
    acceleration = price_5 - price_prior_5_mean

    # RS 斜率（最近 5 日 vs 前 5 日的相对强度变化）
    if n >= 10:
        recent_rs = safe_div(close_vals[-1], float(np.mean(close_vals[-5:-1])), 1.0)
        prior_rs = safe_div(close_vals[-5], float(np.mean(close_vals[-10:-5])), 1.0)
        rs_slope = safe_momentum(recent_rs, prior_rs)
    else:
        rs_slope = 0.0

    # 成交量趋势（最近 5 日量 vs 再前 5 日量）
    if n >= 10:
        vol_recent_5 = np.mean(amount_vals[-5:])
        vol_prior_5 = np.mean(amount_vals[-10:-5])
        vol_trend = safe_momentum(vol_recent_5, vol_prior_5)
    else:
        vol_trend = 0.0

    # 当前阶段持续天数估计
    days_active = _estimate_days_in_phase(close_vals, amount_vals)

    # 价格一致性（有多少天在 MA5 之上）
    if n >= 5:
        above_ma5 = sum(1 for v in close_vals[-5:] if v > ma5)
        consistency = above_ma5 / 5.0
    else:
        consistency = 0

    # ═══════════════════════════════════
    # 2. 6 阶段打分
    # ═══════════════════════════════════

    scores = {
        "ice_recovery": 0.0,
        "startup": 0.0,
        "main_rise_1": 0.0,
        "acceleration": 0.0,
        "high_divergence": 0.0,
        "decay": 0.0,
    }

    # ── 冰点修复 Ice Recovery ──
    # 特征：价格在底部，跌幅但开始缩量，短期开始企稳
    if bias < -5 and price_5 > -3:
        scores["ice_recovery"] += 3
    if bias < -8 and vol_ratio_5_20 < 0.7:
        scores["ice_recovery"] += 4
    if bias < -5 and vol_mom_5 < -15:
        scores["ice_recovery"] += 2
    if bias < -5 and consistency >= 0.6:
        scores["ice_recovery"] += 3

    # ── 启动期 Startup ──
    # 特征：温和放量，价格站上 MA，但尚未大幅上涨
    # ★ 收窄区间：startup 只认初期微弱正偏，避免吞没 main_rise_1
    if 0 < bias < 3 and vol_ratio_5_20 > 1.1:
        scores["startup"] += 3
    if price_5 > 2 and price_5 < 6 and vol_mom_5 > 3:
        scores["startup"] += 3
    if 0 < bias < 3 and vol_ratio_5_20 > 1.0:
        scores["startup"] += 2
    if price_3 > 0 and price_5 > 0 and vol_trend > 0 and bias < 4:
        scores["startup"] += 2
    if consistency >= 0.8 and bias < 3:
        scores["startup"] += 2
    if bias < -2 and price_5 > 0 and vol_mom_5 > 0:
        scores["startup"] += 2  # 超跌反弹转启动

    # ── 主升一期 Main Rise 1 ──
    # 特征：量价齐升，共识形成，中期趋势明确
    # ★ 扩宽触发面：bias>2 即可起步，price_5>3 低门槛
    if bias > 2 and price_5 > 3 and vol_ratio_5_20 > 1.1:
        scores["main_rise_1"] += 5
    if bias > 3 and price_5 > 5 and vol_ratio_5_20 > 1.2:
        scores["main_rise_1"] += 4
    if ma5_over_ma10 > 0.5 and vol_trend > 3:
        scores["main_rise_1"] += 3
    if price_10 > 5 and price_5 > 2:
        scores["main_rise_1"] += 3
    if bias > 2 and consistency >= 0.8:
        scores["main_rise_1"] += 3
    if rs_slope > 0.3 and vol_ratio_5_20 > 1.0:
        scores["main_rise_1"] += 2
    # ★ 中线动量确认：price_20>0 且近5日加速
    if price_20 > 5 and price_5 > 3:
        scores["main_rise_1"] += 3

    # ── 加速期 Acceleration ──
    # 特征：情绪放大，量价极端，趋势加速（过热特征可能已出现）
    if bias > 8 and price_5 > 10:
        scores["acceleration"] += 4
    if price_5 > 8 and acceleration > 3:
        scores["acceleration"] += 4
    if vol_ratio_5_20 > 1.5 and price_5 > 5:
        scores["acceleration"] += 3
    if bias > 6 and price_3 > 3:
        scores["acceleration"] += 3
    if ma5_over_ma10 > 3 and vol_ratio_5_20 > 1.3:
        scores["acceleration"] += 3
    if acceleration > 2:
        scores["acceleration"] += 2

    # ── 高位分歧 High Divergence ──
    # 特征：量价背离，控盘力量减弱，价格开始钝化
    if bias > 5 and vol_mom_5 < -5:
        scores["high_divergence"] += 4
    if price_5 < 2 and vol_ratio_5_20 > 1.0 and bias > 3:
        scores["high_divergence"] += 4
    if price_5 < 0 and bias > 3:
        scores["high_divergence"] += 3
    if acceleration < -2 and bias > 3:
        scores["high_divergence"] += 3
    if consistency < 0.4 and bias > 3:
        scores["high_divergence"] += 3
    if price_10 > 5 and price_5 < 1:
        scores["high_divergence"] += 2

    # ── 退潮期 Decay ──
    # 特征：资金系统性退出，量价齐跌
    if bias < -3 and vol_ratio_5_20 < 0.8:
        scores["decay"] += 4
    if price_5 < -5 and price_10 < -3:
        scores["decay"] += 4
    if price_5 < -3 and vol_trend < -10:
        scores["decay"] += 3
    if bias < -5 and price_10 < -5:
        scores["decay"] += 3
    if price_5 < -3 and consistency < 0.3:
        scores["decay"] += 3

    # ═══════════════════════════════════
    # 3. 分层分类——严格诚实但有兜底
    # ═══════════════════════════════════
    #
    # 原则：
    #   1. 充分证据（≥2.5）→ Softmax 正常分类
    #   2. 弱证据（0.5~2.5）→ 仍归入最高分阶段，但置信度压到低区间
    #   3. 零证据但有有效数据 → noise（横盘/无特征）
    #   4. 数据不足 → unknown（真正的未知）
    #   ★ 绝不返回 "unknown" 当数据充足时，只是信号弱——那叫横盘不是未知

    total_raw = sum(scores.values())

    if total_raw >= 2.5:
        # ── 充分证据 → Softmax 正常分类 ──
        exp_s = {k: math.exp(v) for k, v in scores.items()}
        sum_exp = sum(exp_s.values())
        probs = {k: v / sum_exp for k, v in exp_s.items()}
        phase = max(probs, key=probs.get)
        confidence = round(probs[phase], 2)

        # ★ 防止虚假高置信度：总证据弱时，cap 置信度
        if total_raw < 6:
            confidence = min(confidence, 0.50 + total_raw * 0.08)  # total_raw=2.5→0.70, 5→0.90
        if total_raw < 3:
            confidence = min(confidence, 0.60)

        # ★ 强势期复活逻辑：bias>3 → 自主升一期向上修正
        if bias > 3 and price_5 > 3:
            main_rise_prob = probs.get("main_rise_1", 0)
            accel_prob = probs.get("acceleration", 0)
            if main_rise_prob > 0.15 and phase == "startup":
                phase = "main_rise_1"
                confidence = round(min(main_rise_prob, 0.75), 2)
            elif accel_prob > 0.12 and phase in ("startup", "main_rise_1"):
                phase = "acceleration"
                confidence = round(min(accel_prob, 0.80), 2)
    elif total_raw >= 0.5 and n >= 20:
        # ── 弱证据但数据充足 → Softmax 选最高分，压低可信度 ──
        # 场景：凌晨震荡回调，信号微弱但确实有量价行为可归类
        exp_s = {k: math.exp(v) for k, v in scores.items()}
        sum_exp = sum(exp_s.values())
        probs = {k: v / sum_exp for k, v in exp_s.items()}
        phase = max(probs, key=probs.get)
        confidence = round(min(probs[phase], 0.18 + total_raw * 0.05), 2)  # 0.5→0.20, 2.5→0.30
        # 置信度太低时标注 weak，但前端只显示阶段名称
    elif n >= 30:
        # ── 有充分数据但无任何阶段信号 → 横盘震荡 ──
        if abs(bias) < 3 and abs(price_5) < 2 and abs(price_20) < 3:
            phase = "noise"  # 区分：信号数据但无趋势 = 横盘
            confidence = 0.15
        else:
            # 有数据、无分数但价格有波动 → 也是 noise
            phase = "noise"
            confidence = 0.10
    else:
        # ── 数据太少 → 真正的 unknown ──
        phase = "unknown"
        confidence = 0.0

    # ═══════════════════════════════════
    # 4. 强度分 [0, 100]
    # ═══════════════════════════════════
    strength_score = _compute_strength_score(
        phase, bias, price_5, price_20, vol_ratio_5_20, confidence,
    )

    # ═══════════════════════════════════
    # 5. 拐点检测
    # ═══════════════════════════════════
    is_turning = False
    if phase == "high_divergence" and price_5 < -4:
        is_turning = True
    if phase == "acceleration" and vol_mom_5 < -10:
        is_turning = True
    if phase == "decay" and price_5 > 2 and vol_mom_5 > 5:
        is_turning = True

    return {
        "phase": phase,
        "confidence": confidence,
        "phase_seq": PHASE_ORDER.index(phase) if phase in PHASE_ORDER else -1,
        "scores": scores,
        "bias": round(bias, 2),
        "price_mom_5": round(price_5, 2),
        "price_mom_20": round(price_20, 2),
        "vol_mom_5": round(vol_mom_5, 1),
        "vol_ratio": round(vol_ratio_5_20, 2),
        "acceleration": round(acceleration, 2),
        "rs_slope": round(rs_slope, 2),
        "vol_trend": round(vol_trend, 1),
        "days_active": days_active,
        "consistency": round(consistency, 2),
        "strength_score": round(strength_score, 1),
        "is_turning": is_turning,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
