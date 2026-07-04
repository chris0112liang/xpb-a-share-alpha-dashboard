"""
replay/ — 实盘验证闭环模块

职责：
  盘中/盘后记录 WorldState + 策略 + 候选 + 信号。
  次日验证预测质量 → 修正评分权重。

不启动任何线程。纯被动记录 + 主动验证。
"""

from __future__ import annotations

__version__ = "1.0.0"
