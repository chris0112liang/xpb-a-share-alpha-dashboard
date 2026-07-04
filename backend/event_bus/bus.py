"""EventBus 事件总线（预留骨架）

未来系统从 request/response 升级为 market event driven 架构。
Phase 0 先建好结构，后续填充订阅者逻辑。
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Optional

from schemas import MarketEvent, MarketEventType

logger = logging.getLogger(__name__)

# 事件处理器类型签名
EventHandler = Callable[[MarketEvent], None]


class EventBus:
    """简单事件总线

    用法：
        bus = EventBus()

        # 订阅
        def on_regime_change(event: MarketEvent):
            print(f"Regime changed: {event.payload}")

        bus.subscribe(MarketEventType.REGIME_CHANGED, on_regime_change)

        # 发布
        bus.publish(MarketEvent(
            event_type=MarketEventType.REGIME_CHANGED,
            payload={"from": "chop", "to": "bull_trend"},
        ))
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[MarketEventType, list[EventHandler]] = defaultdict(list)
        self._history: list[MarketEvent] = []
        self._max_history = 1000

    def subscribe(self, event_type: MarketEventType, handler: EventHandler):
        """订阅事件"""
        with self._lock:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: MarketEventType, handler: EventHandler):
        """取消订阅"""
        with self._lock:
            if handler in self._subscribers.get(event_type, []):
                self._subscribers[event_type].remove(handler)

    def publish(self, event: MarketEvent):
        """发布事件——同步调用所有订阅者"""
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            handlers = list(self._subscribers.get(event.event_type, []))

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Event handler failed for {event.event_type}: {e}")

    def publish_async(self, event: MarketEvent):
        """异步发布——在独立线程中执行"""
        thread = threading.Thread(target=self.publish, args=(event,), daemon=True)
        thread.start()

    def get_history(self, event_type: Optional[MarketEventType] = None, limit: int = 50) -> list[MarketEvent]:
        """获取事件历史"""
        with self._lock:
            if event_type:
                return [e for e in self._history if e.event_type == event_type][-limit:]
            return self._history[-limit:]

    def clear(self):
        """清空历史"""
        with self._lock:
            self._history.clear()


# 全局单例
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
