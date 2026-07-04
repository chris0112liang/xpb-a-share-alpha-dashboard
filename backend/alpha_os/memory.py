"""
alpha_os/memory.py — 轻量运行记忆

职责：
  记录 AlphaBrain 每次 tick 的输出变化。
  用于：
  1. 事件去重（同一事件不重复发）
  2. 变化感知（regime 变化时标记）
  3. 趋势判断（连续 tick 的变化方向）

不是持久化存储——重启后重置。
"""

from __future__ import annotations

import logging
from typing import Any
from collections import OrderedDict

logger = logging.getLogger(__name__)


class RunMemory:
    """轻量运行期记忆

    记录最多 N 次 tick 的快照，用于事件去重和变化检测。
    """

    def __init__(self, max_ticks: int = 5):
        self._ticks: list[dict] = []
        self._max_ticks = max_ticks
        self._last_events: set[str] = set()  # 去重用：event_type + sector

    def record(self, snapshot: dict):
        """记录一次 AlphaSnapshot"""
        self._ticks.append(snapshot)
        if len(self._ticks) > self._max_ticks:
            self._ticks = self._ticks[-self._max_ticks:]

        # 更新事件指纹
        current_events: set[str] = set()
        for event in snapshot.get("market_events", []):
            key = f"{event.get('event_type', '')}:{event.get('sector', '')}:{event.get('severity', '')}"
            current_events.add(key)
        self._last_events = current_events

    def has_seen_event(self, event_type: str, sector: str = "", severity: str = "info") -> bool:
        """检查事件是否已经发过"""
        key = f"{event_type}:{sector}:{severity}"
        return key in self._last_events

    def regime_changed(self, new_regime: str) -> tuple[bool, str]:
        """Regime 是否发生了变化"""
        if len(self._ticks) < 2:
            return False, ""
        prev = self._ticks[-2]
        prev_regime = prev.get("world_state", {}).get("regime", "")
        old = prev_regime.value if hasattr(prev_regime, 'value') else str(prev_regime)
        new = new_regime.value if hasattr(new_regime, 'value') else str(new_regime)
        if old and new and old != new:
            return True, f"{old} → {new}"
        return False, ""

    def rotation_trend(self) -> str:
        """轮动速度趋势：rising / falling / stable"""
        if len(self._ticks) < 2:
            return "stable"
        speeds = [
            t.get("world_state", {}).get("rotation_speed", 0.5)
            for t in self._ticks[-3:]
        ]
        if len(speeds) < 2:
            return "stable"
        if speeds[-1] > speeds[0] * 1.05:
            return "rising"
        elif speeds[-1] < speeds[0] * 0.95:
            return "falling"
        return "stable"

    def clear(self):
        self._ticks.clear()
        self._last_events.clear()


# 全局实例
_run_memory = RunMemory()


def get_memory() -> RunMemory:
    return _run_memory
