"""DataBus 缓存管理器

3 层缓存策略（Tier 1/2/3）
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LRUCache:
    """内存 LRU 缓存——用于 Tier 1 & Tier 2"""

    def __init__(self, max_size: int = 5000, ttl_seconds: int = 300):
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None
        ts, value = self._cache[key]
        if time.time() - ts > self._ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: Any):
        if len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = (time.time(), value)

    def clear(self):
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


# Tier 1: 实时高频数据（TOP 3000 流动性股）
_tier1_cache = LRUCache(max_size=5000, ttl_seconds=300)  # 5 分钟过期

# Tier 2: 中等频率（板块/因子/热门股）
_tier2_cache = LRUCache(max_size=2000, ttl_seconds=1800)  # 30 分钟过期

# Tier 3: 离线数据（全市场全历史因子）
# Tier 3 不走内存缓存，直接读 DuckDB/Parquet


def get_tier1(key: str) -> Optional[Any]:
    return _tier1_cache.get(key)


def set_tier1(key: str, value: Any):
    _tier1_cache.set(key, value)


def get_tier2(key: str) -> Optional[Any]:
    return _tier2_cache.get(key)


def set_tier2(key: str, value: Any):
    _tier2_cache.set(key, value)


def clear_all():
    _tier1_cache.clear()
    _tier2_cache.clear()
