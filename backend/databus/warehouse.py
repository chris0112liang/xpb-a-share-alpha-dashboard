"""DuckDB + Parquet 数据仓库层

所有历史数据存储在这里。
上层读数据走 warehouse，写数据也走 warehouse。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from schemas import MarketBar, Timeframe

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).parent.parent / "data" / "market"
DAILY_DIR = DATA_ROOT / "daily"
SECTOR_DIR = DATA_ROOT / "sector"
DB_PATH = DATA_ROOT / "alpha_os.db"


class MarketWarehouse:
    """市场数据仓库

    用法：
        wh = MarketWarehouse()
        wh.init_db()

        # 写入日线
        wh.store_daily_bars(bars, year=2026)

        # 查询日线
        bars = wh.query_daily_bars("000001", start_date="20250101")

        # 世界状态查询
        ws = wh.get_recent_weekly_data("000300", weeks=12)
    """

    def __init__(self):
        self._conn: Optional[duckdb.DuckDBPyConnection] = None

        # 确保目录存在
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        SECTOR_DIR.mkdir(parents=True, exist_ok=True)
        DATA_ROOT.mkdir(parents=True, exist_ok=True)

    # ── 连接管理 ──

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(str(DB_PATH))
            self._conn.execute("SET threads=1")                          # 单线程防死锁
            self._conn.execute("PRAGMA disable_profiling")               # 减少落盘
            self._conn.execute("SET enable_external_access=false")       # 禁止外部文件访问
        return self._conn

    def init_db(self):
        """初始化 DuckDB——创建视图和宏"""
        c = self.conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock_meta (
                code VARCHAR PRIMARY KEY,
                name VARCHAR,
                sector VARCHAR,
                list_date DATE,
                total_shares DOUBLE,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_sync_log (
                code VARCHAR,
                date DATE,
                status VARCHAR,
                bars_count INTEGER,
                sync_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (code, date)
            )
        """)

    # ── Parquet 写入 ──

    def store_daily_bars(self, bars: list[MarketBar], year: Optional[int] = None):
        """将日线分批写入 Parquet 分区

        分区路径：data/market/daily/year=YYYY/code.parquet
        """
        if not bars:
            return

        # 按年份分组
        by_year: dict[int, list[MarketBar]] = {}
        for bar in bars:
            y = bar.timestamp.year
            if year is not None and y != year:
                continue
            by_year.setdefault(y, []).append(bar)

        for y, ybars in by_year.items():
            year_dir = DAILY_DIR / f"year={y}"
            year_dir.mkdir(parents=True, exist_ok=True)

            # 按 code 分组写入
            by_code: dict[str, list[MarketBar]] = {}
            for bar in ybars:
                by_code.setdefault(bar.code, []).append(bar)

            for code, code_bars in by_code.items():
                filepath = year_dir / f"{code}.parquet"

                # 构造 pyarrow table
                table = pa.table({
                    "code": pa.array([b.code for b in code_bars], type=pa.string()),
                    "timestamp": pa.array([b.timestamp for b in code_bars], type=pa.timestamp("ms")),
                    "open": pa.array([b.open for b in code_bars], type=pa.float64()),
                    "high": pa.array([b.high for b in code_bars], type=pa.float64()),
                    "low": pa.array([b.low for b in code_bars], type=pa.float64()),
                    "close": pa.array([b.close for b in code_bars], type=pa.float64()),
                    "volume": pa.array([b.volume for b in code_bars], type=pa.float64()),
                    "amount": pa.array([b.amount for b in code_bars], type=pa.float64()),
                    "change_pct": pa.array([b.change_pct for b in code_bars], type=pa.float64()),
                    "turnover_pct": pa.array([b.turnover_pct for b in code_bars], type=pa.float64()),
                    "adj_factor": pa.array([b.adj_factor for b in code_bars], type=pa.float64()),
                })

                # 写入（追加到已有文件）
                if filepath.exists():
                    existing = pq.read_table(str(filepath))
                    combined = pa.concat_tables([existing, table])
                    pq.write_table(combined, str(filepath))
                else:
                    pq.write_table(table, str(filepath))

                # DuckDB 元数据
                self.conn.execute(
                    "INSERT OR REPLACE INTO daily_sync_log (code, date, status, bars_count) VALUES (?, ?, ?, ?)",
                    [code, datetime(y, 1, 1).date(), "stored", len(code_bars)],
                )

            logger.info(f"Stored {len(ybars)} bars to {year_dir}")

    # ── Parquet 查询 ──

    def query_daily_bars(
        self,
        code: str,
        start_date: str = "20050101",
        end_date: Optional[str] = None,
    ) -> list[MarketBar]:
        """从 Parquet 分区查询日线"""
        # 拼接日期范围对应年份
        start_year = int(start_date[:4])
        end_year = int((end_date or datetime.now().strftime("%Y%m%d"))[:4])

        bars = []
        for year in range(start_year, end_year + 1):
            year_dir = DAILY_DIR / f"year={year}"
            filepath = year_dir / f"{code}.parquet"
            if not filepath.exists():
                continue

            try:
                table = pq.read_table(str(filepath))
                df = table.to_pandas()
            except Exception:
                continue

            if df.empty:
                continue

            # 日期过滤
            df["ts"] = pd.to_datetime(df["timestamp"])
            if start_date:
                df = df[df["ts"] >= pd.Timestamp(start_date)].copy()
            if end_date:
                df = df[df["ts"] <= pd.Timestamp(end_date)].copy()

            for _, row in df.iterrows():
                bars.append(MarketBar(
                    code=str(row.get("code", code)),
                    timeframe=Timeframe.DAY,
                    timestamp=row["ts"].to_pydatetime(),
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("volume", 0)),
                    amount=float(row.get("amount", 0)),
                    change_pct=float(row.get("change_pct", 0)),
                    turnover_pct=float(row.get("turnover_pct", 0)),
                ))

        return bars

    def has_data(self, code: str, year: int) -> bool:
        """检查是否已有某年数据"""
        filepath = DAILY_DIR / f"year={year}" / f"{code}.parquet"
        if filepath.exists():
            try:
                table = pq.read_table(str(filepath))
                return table.num_rows > 0
            except Exception:
                return False
        return False

    def get_all_stored_codes(self) -> set[str]:
        """获取所有已存储的股票代码"""
        codes = set()
        for year_dir in sorted(DAILY_DIR.iterdir()):
            if not year_dir.is_dir():
                continue
            for f in year_dir.glob("*.parquet"):
                codes.add(f.stem)
        return codes

    def get_sync_status(self) -> dict:
        """获取同步状态"""
        result = self.conn.execute(
            "SELECT code, MAX(date) as last_date, COUNT(*) as years FROM daily_sync_log GROUP BY code"
        ).fetchall()
        return {
            "total_codes": len(result),
            "latest_sync": max((r[1] for r in result), default=None),
            "codes_with_data": [r[0] for r in result],
        }

    def get_year_range(self) -> tuple[int, int]:
        """获取数据年份范围"""
        years = [int(d.name.split("=")[1]) for d in DAILY_DIR.iterdir() if d.is_dir() and d.name.startswith("year=")]
        if not years:
            return (0, 0)
        return (min(years), max(years))

    # ── 市场状态辅助查询 ──

    def get_recent_bars(self, code: str, days: int = 60) -> list[MarketBar]:
        """获取最近 N 天日线"""
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
        bars = self.query_daily_bars(code, start_date=start, end_date=end)
        return bars[-days:] if len(bars) > days else bars

    def get_market_breadth_from_db(self) -> dict:
        """从数据库计算市场宽度（placeholder——实际走实时）"""
        # TODO: 从全数据表格计算涨跌家数比
        return {"advance": 0, "decline": 0, "breadth": 0.0}

    def close(self):
        """关闭连接"""
        if self._conn:
            self._conn.close()
            self._conn = None


# 全局单例
_warehouse: Optional[MarketWarehouse] = None


def get_warehouse() -> MarketWarehouse:
    global _warehouse
    if _warehouse is None:
        _warehouse = MarketWarehouse()
        _warehouse.init_db()
    return _warehouse
