"""Signals 层——Regime Engine 的输入信号计算

信号层只负责计算原始数值。
不解释、不推理、不输出 regime。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from databus import DataBus
from schemas import SignalGroup, FactorType, MarketRegime

logger = logging.getLogger(__name__)


class MarketSignals:
    """市场信号计算器

    从 DataBus 获取原始数据 → 计算出所有信号值 → 返回 SignalGroup
    """

    def __init__(self, bus: Optional[DataBus] = None):
        self._bus = bus or DataBus()

    def compute_all(self) -> SignalGroup:
        """计算所有信号——单次调用"""
        return SignalGroup(
            breadth_score=self._compute_breadth(),
            volatility_score=self._compute_volatility(),
            turnover_score=self._compute_turnover(),
            liquidity_score=self._compute_liquidity(),
            momentum_dispersion=self._compute_momentum_dispersion(),
            sector_concentration=self._compute_sector_concentration(),
            top_line_strength=self._compute_top_line_strength(),
        )

    def _compute_breadth(self) -> float:
        """涨跌家数比 → [-1, 1]

        0.6+ = 市场宽度好（普涨）
        -0.6以下 = 市场宽度差（普跌）
        """
        try:
            data = self._bus.get_breadth()
            return float(data.get("breadth", 0.0))
        except Exception:
            return 0.0

    def _compute_volatility(self) -> float:
        """波动率水平 → [0, 1]

        用上证指数最近 20 日真实波幅（ATR / close）计算
        0.8+ = 高波动, 0.3- = 低波动
        """
        try:
            bars = self._bus.get_index_bars("000001", start_date=(datetime.now() - timedelta(days=40)).strftime("%Y%m%d"))
            if len(bars) < 10:
                return 0.5

            closes = np.array([b.close for b in bars[-20:]])
            highs = np.array([b.high for b in bars[-20:]])
            lows = np.array([b.low for b in bars[-20:]])

            # TR（真实波幅）
            prev_close = np.roll(closes, 1)
            prev_close[0] = closes[0]
            tr = np.maximum(
                highs - lows,
                np.maximum(
                    np.abs(highs - prev_close),
                    np.abs(lows - prev_close),
                ),
            )
            atr_ratio = np.mean(tr) / np.mean(closes)

            # 归一化：假设正常 ATR 比 1%-2%, 3%+ 是高波动
            vol_score = min(1.0, max(0.0, atr_ratio * 50))
            return round(float(vol_score), 4)
        except Exception:
            return 0.5

    def _compute_turnover(self) -> float:
        """换手率水平 → [0, 1]

        全市场成交额与 20 日均值的比值
        """
        try:
            bars = self._bus.get_index_bars("000001", start_date=(datetime.now() - timedelta(days=40)).strftime("%Y%m%d"))
            if len(bars) < 5:
                return 0.5

            amounts = np.array([b.amount for b in bars[-20:]])
            current = amounts[-1]
            avg = np.mean(amounts[:-1]) if len(amounts) > 1 else current
            ratio = current / avg if avg > 0 else 1.0

            # ratio > 1.5 = 放量, ratio < 0.6 = 缩量
            turnover_score = min(1.0, max(0.0, (ratio - 0.4) / 1.2))
            return round(float(turnover_score), 4)
        except Exception:
            return 0.5

    def _compute_liquidity(self) -> float:
        """流动性水平 → [0, 1]

        用全市场成交额的移动斜率判断
        """
        try:
            bars = self._bus.get_index_bars("000001", start_date=(datetime.now() - timedelta(days=40)).strftime("%Y%m%d"))
            if len(bars) < 10:
                return 0.5

            amounts = np.array([b.amount for b in bars[-10:]])
            if np.std(amounts) == 0:
                return 0.5

            # 线性回归斜率
            x = np.arange(len(amounts))
            slope = np.polyfit(x, amounts, 1)[0] / (np.mean(amounts) + 1e-8)
            # 归一化到 [0, 1]
            liquidity_score = min(1.0, max(0.0, (slope * 100 + 0.5)))
            return round(float(liquidity_score), 4)
        except Exception:
            return 0.5

    def _compute_momentum_dispersion(self) -> float:
        """动量分散度 → [0, 1]

        板块间动量差异程度
        0.2- = 动量集中（有明确主线）
        0.8+ = 动量高度分散（轮动电风扇）
        """
        try:
            # 用板块 ETF 作为 proxy——获取板块 5 日收益的离散程度
            top_sectors = ["半导体", "通信", "新能源车", "医疗", "消费"]
            returns = []

            for sector_name in top_sectors[:3]:  # 取 3 个板块
                # TODO: 从 DataBus 获取板块日线
                pass

            # 简版：如果涨跌家数宽度极好或极差，动量集中
            breadth = self._compute_breadth()
            if abs(breadth) > 0.5:
                return 0.2  # 动量集中

            # 查不到板块数据时，用指数内部板块分化来估计
            return 0.6  # 保守估计中度分散
        except Exception:
            return 0.5

    def _compute_sector_concentration(self) -> float:
        """板块集中度 → [0, 1]

        是否有明确主线板块
        0.8+ = 高度集中（明确主线）
        0.3- = 无明确主线
        """
        try:
            # 从板块扫描器获取当前板块生命周期数据
            from sector_worker import SECTOR_LIFECYCLE_FULL
            sectors = SECTOR_LIFECYCLE_FULL

            if not sectors:
                return 0.5

            strengthening = sum(
                1 for v in sectors.values()
                if isinstance(v, dict) and v.get("phase") == "strengthening"
            )

            # 强化期板块占比高 = 主线明确
            total = len(sectors)
            ratio = strengthening / total if total > 0 else 0
            return round(float(min(1.0, ratio * 5)), 4)  # 20% 强化期 = 1.0
        except Exception:
            return 0.5

    def _compute_top_line_strength(self) -> float:
        """主线强度 → [0, 1]

        最强板块的动量强度
        """
        try:
            from sector_worker import SECTOR_LIFECYCLE_FULL
            sectors = SECTOR_LIFECYCLE_FULL
            if not sectors:
                return 0.5

            # 找 bias 最大的板块
            max_bias = 0.0
            for v in sectors.values():
                if isinstance(v, dict) and "bias" in v:
                    bias = abs(v["bias"])
                    if bias > max_bias:
                        max_bias = bias

            # bias 归一化: bias > 8 为强主线
            return round(float(min(1.0, max_bias / 12)), 4)
        except Exception:
            return 0.5
