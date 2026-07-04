"""DataBus 抽象基类 + 统一 Provider 接口

所有 Provider 继承 BaseProvider，DataBus 调度它们。
上层代码完全不感知数据来源。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from schemas import MarketBar, Timeframe

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """数据提供者抽象基类"""

    name: str = "base"

    @abstractmethod
    def name(self) -> str:
        ...

    # ── 日线行情 ──

    @abstractmethod
    def fetch_daily_bars(
        self,
        code: str,
        start_date: str = "20050101",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> list[MarketBar]:
        """获取个股日线行情"""
        ...

    @abstractmethod
    def fetch_index_daily(
        self,
        code: str,
        start_date: str = "20050101",
        end_date: Optional[str] = None,
    ) -> list[MarketBar]:
        """获取指数日线行情"""
        ...

    # ── 实时行情 ──

    @abstractmethod
    def fetch_realtime_quote(self, code: str) -> Optional[MarketBar]:
        """获取实时行情"""
        ...

    @abstractmethod
    def fetch_batch_realtime(self, codes: list[str]) -> list[MarketBar]:
        """批量实时行情"""
        ...

    # ── 板块数据 ──

    @abstractmethod
    def fetch_sector_daily(self, sector_code: str, days: int = 60) -> list[MarketBar]:
        """获取板块日线"""
        ...

    # ── 资金流 ──

    @abstractmethod
    def fetch_money_flow(self, code: str, days: int = 30) -> list[dict]:
        """资金流向"""
        ...

    # ── 北向资金 ──

    @abstractmethod
    def fetch_northbound_flow(self) -> dict:
        """北向资金总览"""
        ...

    # ── 市场宽度 ──

    @abstractmethod
    def fetch_market_breadth(self) -> dict:
        """涨跌家数"""
        ...


class ProviderError(Exception):
    """数据提供者异常"""
    pass
