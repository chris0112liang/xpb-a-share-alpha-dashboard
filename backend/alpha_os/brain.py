"""
alpha_os/brain.py — AlphaBrain

系统核心入口。

tick() 完成后自动记录快照到 replay 系统。

用法：
  from alpha_os import AlphaBrain
  snapshot = AlphaBrain.tick()   # → 返回快照 + 自动记录 replay

自动记录的内容：
  - regime / rotation / risk
  - active_strategies
  - top_candidates (10只)
  - market_events
  - 每人一个 snapshot_id (20260526-01)

警告：
  - 不要在 __init__ 或 import 时 import recorder
    避免循环：brain → recorder → brain
  - 使用懒加载只在 tick() 时 import
"""

from __future__ import annotations

import logging
from typing import Any

from .orchestrator import tick as _orchestrate
from .memory import get_memory, RunMemory

logger = logging.getLogger(__name__)


class AlphaBrain:
    """Alpha 大脑——AI Alpha OS 的核心

    tick() 是唯一入口。
    输出 AlphaSnapshot 是唯一输出格式。
    每次 tick() 结束后自动记录 replay snapshot。
    """

    _instance: AlphaBrain | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._mem = get_memory()
            self._last_snapshot: dict | None = None
            self._initialized = True

    @classmethod
    def tick(cls) -> dict:
        """执行一次完整大脑心跳

        调用方式：AlphaBrain.tick()
        幂等，每次返回当前快照
        自动记录 replay
        """
        instance = cls()
        snapshot = _orchestrate()
        instance._last_snapshot = snapshot
        # 自动记录 replay（懒加载避免循环 import）
        try:
            from replay.recorder import record_snapshot as _record
            _record(snapshot)
        except Exception as e:
            logger.warning(f"[AlphaBrain] replay record failed: {e}")
        return snapshot

    @classmethod
    def last_snapshot(cls) -> dict | None:
        """获取上一次 tick 的输出"""
        instance = cls()
        return instance._last_snapshot

    @classmethod
    def clear_memory(cls):
        """清空运行记忆"""
        instance = cls()
        instance._mem.clear()
        logger.info("[AlphaBrain] run memory cleared")


# 便捷引用
brain_tick = AlphaBrain.tick
