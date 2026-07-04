"""DataBus 统一数据总线

DataBus 是系统中所有数据的唯一入口。
上层代码（Regime Engine / Sector / API）只和 DataBus 交谈。

使用方式：
    bus = DataBus()
    bars = bus.get_daily_bars("000001")
    breadth = bus.get_breadth()
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from schemas import MarketBar, Timeframe
from databus.base import BaseProvider, ProviderError
from databus.cache import get_tier1, set_tier1, get_tier2, set_tier2, clear_all
from databus.warehouse import get_warehouse, MarketWarehouse
from databus.providers.akshare_provider import AKShareProvider

logger = logging.getLogger(__name__)


class DataBus:
    """统一数据总线

    内部策略：
    1. 写：实时数据 → AKShare → Tier1/2 缓存
    2. 读：Tier1 缓存 → AKShare → DuckDB/Parquet（长历史）
    3. 回填：长历史 → DuckDB/Parquet 异步写入
    """

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {
            "akshare": AKShareProvider(),
        }
        self._warehouse: MarketWarehouse = get_warehouse()
        self._executor = ThreadPoolExecutor(max_workers=4)

    @property
    def warehouse(self) -> MarketWarehouse:
        return self._warehouse

    # ── 个股日线 ──

    def get_daily_bars(
        self,
        code: str,
        start_date: str = "20200101",
        end_date: Optional[str] = None,
        use_cache: bool = True,
    ) -> list[MarketBar]:
        """获取个股日线

        策略：先读 Parquet → 无数据则实时拉取
        """
        cache_key = f"daily:{code}:{start_date}:{end_date or 'now'}"

        if use_cache:
            cached = get_tier2(cache_key)
            if cached is not None:
                return cached

        # 尝试从仓库读取
        bars = self._warehouse.query_daily_bars(code, start_date=start_date, end_date=end_date)
        if bars:
            if use_cache:
                set_tier2(cache_key, bars)
            return bars

        # 仓库无数据 → AKShare 实时拉取
        provider = self._providers.get("akshare")
        if not provider:
            return []

        bars = provider.fetch_daily_bars(code, start_date=start_date, end_date=end_date)
        if bars:
            # 异步回填仓库
            self._executor.submit(self._warehouse.store_daily_bars, bars)
            if use_cache:
                set_tier2(cache_key, bars)

        return bars

    # ── 指数日线 ──

    def get_index_bars(
        self,
        code: str,
        start_date: str = "20200101",
        end_date: Optional[str] = None,
    ) -> list[MarketBar]:
        """获取指数日线"""
        provider = self._providers.get("akshare")
        if not provider:
            return []
        return provider.fetch_index_daily(code, start_date=start_date, end_date=end_date)

    # ── 板块日线 ──

    def get_sector_bars(self, sector_code: str, days: int = 60) -> list[MarketBar]:
        """获取板块日线"""
        cache_key = f"sector_daily:{sector_code}:{days}"
        cached = get_tier2(cache_key)
        if cached is not None:
            return cached

        provider = self._providers.get("akshare")
        if not provider:
            return []
        bars = provider.fetch_sector_daily(sector_code, days=days)
        if bars:
            set_tier2(cache_key, bars)
        return bars

    # ── 实时行情 ──

    def get_realtime(self, code: str) -> Optional[MarketBar]:
        """获取实时行情"""
        cache_key = f"realtime:{code}"
        cached = get_tier1(cache_key)
        if cached is not None:
            return cached

        provider = self._providers.get("akshare")
        if not provider:
            return None
        bar = provider.fetch_realtime_quote(code)
        if bar:
            set_tier1(cache_key, bar)
        return bar

    def get_batch_realtime(self, codes: list[str]) -> list[MarketBar]:
        """批量实时行情"""
        # 先查缓存
        uncached = []
        for code in codes:
            cached = get_tier1(f"realtime:{code}")
            if cached is None:
                uncached.append(code)

        if uncached:
            provider = self._providers.get("akshare")
            if provider:
                fresh = provider.fetch_batch_realtime(uncached)
                for bar in fresh:
                    set_tier1(f"realtime:{bar.code}", bar)
                return fresh

        return []

    # ── 市场信号 ──

    def get_breadth(self) -> dict:
        """涨跌家数"""
        cached = get_tier1("breadth")
        if cached is not None:
            return cached

        provider = self._providers.get("akshare")
        if not provider:
            return {"advance": 0, "decline": 0, "flat": 0, "breadth": 0.0}
        data = provider.fetch_market_breadth()
        total = data.get("total", 1) or 1
        data["breadth"] = data.get("breadth", (data.get("advance", 0) - data.get("decline", 0)) / total)
        set_tier1("breadth", data)
        return data

    def get_northbound(self) -> dict:
        """北向资金"""
        cached = get_tier1("northbound")
        if cached is not None:
            return cached

        provider = self._providers.get("akshare")
        if not provider:
            return {"north_net_in": 0}
        data = provider.fetch_northbound_flow()
        set_tier1("northbound", data)
        return data

    def get_top_3000(self) -> list[str]:
        """获取 TOP 3000 流动性股票"""
        cached = get_tier1("top_3000")
        if cached is not None:
            return cached

        provider = self._providers.get("akshare")
        if not provider:
            return []
        codes = provider.fetch_top_3000_by_volume()
        if codes:
            set_tier1("top_3000", codes)
        return codes

    # ── 批量历史数据拉取（Tier 3 夜间任务） ──

    def backfill_daily_history(
        self,
        codes: list[str],
        start_date: str = "20050101",
        max_workers: int = 3,
    ) -> dict:
        """批量回填全历史日线

        用于夜间离线任务（Tier 3）
        """
        provider = self._providers.get("akshare")
        if not provider:
            return {"success": 0, "failed": 0, "total": len(codes)}

        results = {"success": 0, "failed": 0, "total": len(codes), "skipped": 0}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for code in codes:
                # 检查是否已有
                current_year = datetime.now().year
                if self._warehouse.has_data(code, current_year):
                    results["skipped"] += 1
                    continue
                f = executor.submit(provider.fetch_daily_bars, code, start_date)
                futures[f] = code

            for f in as_completed(futures):
                code = futures[f]
                try:
                    bars = f.result()
                    if bars:
                        self._warehouse.store_daily_bars(bars)
                        results["success"] += 1
                    else:
                        results["failed"] += 1
                except Exception:
                    results["failed"] += 1

                if (results["success"] + results["failed"]) % 100 == 0:
                    logger.info(
                        f"Backfill progress: {results['success']}/{results['total']} "
                        f"(failed: {results['failed']}, skipped: {results['skipped']})"
                    )

        return results

    def get_all_codes(self) -> list[str]:
        """从 STOCK_SECTOR_MAP 获取全市场股票代码"""
        import json
        map_path = __file__.replace("databus/__init__.py", "STOCK_SECTOR_MAP.json").replace("databus/", "")
        try:
            with open(map_path) as f:
                data = json.load(f)
            return list(data.keys())
        except Exception:
            logger.warning("Cannot load STOCK_SECTOR_MAP.json")
            return []
