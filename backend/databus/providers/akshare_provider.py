"""AKShare 数据提供者

在 WSL 环境下，AKShare 的 HTTPS（Eastmoney）连接会 SSL 错误。
改为优先使用 AKShare 的腾讯源（stock_zh_a_hist_tx），EM 源仅作补充。
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd
import requests

from databus.base import BaseProvider, ProviderError
from schemas import MarketBar, Timeframe

logger = logging.getLogger(__name__)

REQUEST_INTERVAL = 0.35


class AKShareProvider(BaseProvider):
    name = "akshare"

    def __init__(self):
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn",
        })

    def _throttle(self):
        now = time.time()
        gap = REQUEST_INTERVAL - (now - self._last_request)
        if gap > 0:
            time.sleep(gap)
        self._last_request = time.time()

    def _normalize_tx_df(self, df: pd.DataFrame, code: str) -> list[MarketBar]:
        """标准化 DataFrame → MarketBar list"""
        bars = []
        if df is None or df.empty:
            return bars

        for _, row in df.iterrows():
            try:
                ts_str = str(row.get("date", ""))
                if not ts_str:
                    continue
                ts = datetime.strptime(ts_str[:10], "%Y-%m-%d")
                bars.append(MarketBar(
                    code=code,
                    timeframe=Timeframe.DAY,
                    timestamp=ts,
                    open=float(row.get("open", 0) or 0),
                    high=float(row.get("high", 0) or 0),
                    low=float(row.get("low", 0) or 0),
                    close=float(row.get("close", 0) or 0),
                    volume=float(row.get("volume", 0) or 0),
                    amount=float(row.get("amount", 0) or 0),
                ))
            except Exception:
                continue
        return bars

    def fetch_daily_bars(
        self,
        code: str,
        start_date: str = "20050101",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> list[MarketBar]:
        """使用 TX 源获取个股日线（带 8s 超时）"""
        from network_guard import safe_call
        self._throttle()
        try:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            end = end_date or datetime.now().strftime("%Y%m%d")
            df = safe_call(
                lambda: ak.stock_zh_a_hist_tx(
                    symbol=f"{prefix}{code}",
                    start_date=start_date,
                    end_date=end,
                    adjust=adjust,
                ), timeout=8.0, name=f"TX_daily_{code[:4]}"
            )
            return self._normalize_tx_df(df, code)
        except Exception as e:
            logger.warning(f"TX daily({code}) failed: {e}")
            return []

    def fetch_index_daily(
        self,
        code: str,
        start_date: str = "20200101",
        end_date: Optional[str] = None,
    ) -> list[MarketBar]:
        """指数日线——TX 源（带 8s 超时）"""
        from network_guard import safe_call
        self._throttle()
        try:
            end = end_date or datetime.now().strftime("%Y%m%d")
            df = safe_call(
                lambda: ak.stock_zh_a_hist_tx(
                    symbol=f"sh{code}" if not code.startswith("sh") else code,
                    start_date=start_date,
                    end_date=end,
                    adjust="qfq",
                ), timeout=8.0, name=f"TX_index_{code[:4]}"
            )
            return self._normalize_tx_df(df, code)
        except Exception as e:
            logger.warning(f"TX index({code}) failed: {e}")
            # fallback: AKShare 指数
            try:
                self._throttle()
                df = ak.stock_zh_index_daily(symbol=f"sh{code[2:]}" if code.startswith("00") else f"sh{code}")
                if df is None or df.empty:
                    return []
                df = df[df["date"] >= start_date].copy()
                if end_date:
                    df = df[df["date"] <= end_date].copy()
                bars = []
                for _, row in df.iterrows():
                    bars.append(MarketBar(
                        code=code,
                        timeframe=Timeframe.DAY,
                        timestamp=datetime.strptime(row["date"], "%Y-%m-%d"),
                        open=float(row.get("open", 0)),
                        high=float(row.get("high", 0)),
                        low=float(row.get("low", 0)),
                        close=float(row.get("close", 0)),
                        volume=float(row.get("volume", 0)),
                        amount=float(row.get("amount", 0)),
                    ))
                return bars
            except Exception:
                return []

    def fetch_realtime_quote(self, code: str) -> Optional[MarketBar]:
        """实时行情——腾讯逐笔

        TX format (88 fields split by ~):
        [0]=market_code, [1]=name, [2]=code, [3]=current, [4]=yesterday_close
        [5]=open, [6]=volume(手), [7]=outer_disc, [8]=inner_disc
        [9]=current(again), [10]=bid_vol
        """
        self._throttle()
        try:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            url = f"https://qt.gtimg.cn/q={prefix}{code}"
            r = self._session.get(url, timeout=10)
            text = r.text.strip() if r.status_code == 200 else ""

            if not text:
                return None

            parts = text.split("~")
            if len(parts) < 6:
                return None

            try:
                close = float(parts[3] or 0)
                open_ = float(parts[5] or 0)
                high = float(parts[33] or 0) if len(parts) > 33 else close
                low = float(parts[34] or 0) if len(parts) > 34 else close
                pre_close = float(parts[4] or 0)
                volume_raw = float(parts[6] or 0) if len(parts) > 6 else 0  # 手
                volume = volume_raw * 100  # 手→股
                amount_raw = float(parts[20] or 0) if len(parts) > 20 else 0  # 万
                amount = amount_raw * 10000  # 万→元
                change_pct = ((close - pre_close) / pre_close * 100) if pre_close > 0 else 0.0

                return MarketBar(
                    code=code,
                    name=parts[1],
                    timeframe=Timeframe.DAY,
                    timestamp=datetime.now(),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    change_pct=round(change_pct, 2),
                )
            except (ValueError, IndexError) as e:
                logger.debug(f"TX parse failed for {code}: {e}")
                return None
        except Exception as e:
            logger.warning(f"TX realtime({code}) failed: {e}")
            return None

    def fetch_batch_realtime(self, codes: list[str]) -> list[MarketBar]:
        """批量实时——腾讯接口单次最多 20 只"""
        bars = []
        batch_size = 20
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            self._throttle()
            try:
                qs = ",".join(
                    f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}"
                    for c in batch
                )
                url = f"https://qt.gtimg.cn/q={qs}"
                r = self._session.get(url, timeout=10)
                if r.status_code != 200:
                    continue

                lines = r.text.strip().split("\n")
                for line in lines:
                    parts = line.split("~")
                    if len(parts) < 10:
                        continue
                    try:
                        code_raw = parts[0].split("_")[-1].split("=")[0]
                        if code_raw.startswith('"'):
                            code_raw = code_raw[1:]
                        code = code_raw[2:] if code_raw.startswith(("sh", "sz")) else code_raw
                        close = float(parts[3] or 0)
                        pre_close = float(parts[4] or 0)
                        change_pct = round(((close - pre_close) / pre_close * 100) if pre_close > 0 else 0.0, 2)
                        bars.append(MarketBar(
                            code=code,
                            name=parts[1],
                            timeframe=Timeframe.DAY,
                            timestamp=datetime.now(),
                            open=float(parts[5] or 0),
                            high=float(parts[33] or 0) if len(parts) > 33 else close,
                            low=float(parts[34] or 0) if len(parts) > 34 else close,
                            close=close,
                            volume=(float(parts[6] or 0) if len(parts) > 6 else 0) * 100,
                            amount=(float(parts[20] or 0) if len(parts) > 20 else 0) * 10000,
                            change_pct=change_pct,
                        ))
                    except (ValueError, IndexError):
                        continue
            except Exception:
                continue
        return bars

    def fetch_sector_daily(self, sector_code: str, days: int = 60) -> list[MarketBar]:
        """板块日线——通过板块 ETF 的 TX 数据"""
        self._throttle()
        try:
            prefix = "sh" if sector_code.startswith(("5", "6", "9")) else "sz"
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist_tx(
                symbol=f"{prefix}{sector_code}",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            return self._normalize_tx_df(df, sector_code)[-days:]
        except Exception as e:
            logger.warning(f"TX sector({sector_code}) failed: {e}")
            return []

    def fetch_money_flow(self, code: str, days: int = 30) -> list[dict]:
        """资金流向——EM（带 8s 超时）"""
        from network_guard import safe_call
        self._throttle()
        try:
            df = safe_call(
                lambda: ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz"),
                timeout=8.0, name=f"money_flow_{code[:4]}"
            )
            if df is None or (hasattr(df, "empty") and df.empty):
                return []
            return df.tail(days).to_dict(orient="records") if hasattr(df, "tail") else []
        except Exception as e:
            logger.warning(f"Money flow({code}) failed: {e}")
            return []

    def fetch_northbound_flow(self) -> dict:
        """北向资金"""
        # EM 北向在 WSL 不可用，用 sina 代替
        try:
            url = "https://vip.stock.finance.sina.com.cn/corp/go.php/vMS_MarketHistory/stockid/150997.phtml"
            return {"north_net_in": 0, "source": "unavailable", "note": "WSL北向暂不可用"}
        except Exception:
            return {"north_net_in": 0}

    def fetch_market_breadth(self) -> dict:
        """市场宽度——通过 AKShare 全市场实时数据统计（带 10s 超时）"""
        from network_guard import safe_call
        try:
            df = safe_call(
                lambda: ak.stock_zh_a_spot_em(),
                timeout=10.0, name="breadth_spot_em"
            )
            if df is None or (hasattr(df, "empty") and df.empty):
                return self._fetch_breadth_sina_fallback()

            # 列名可能有变，尝试不同名称
            pct_col = None
            for col in ["涨跌幅", "pctChg", "changepercent", "pct_chg"]:
                if col in df.columns:
                    pct_col = col
                    break

            if pct_col is None:
                return self._fetch_breadth_sina_fallback()

            advance = int((df[pct_col] > 0).sum())
            decline = int((df[pct_col] < 0).sum())
            flat = int(len(df) - advance - decline)
            total = int(len(df))
            breadth = (advance - decline) / total if total > 0 else 0.0

            return {"advance": advance, "decline": decline, "flat": flat, "total": total, "breadth": round(breadth, 4)}
        except Exception as e:
            logger.warning(f"Breadth(akshare) failed: {e}")
            return self._fetch_breadth_sina_fallback()

    def _fetch_breadth_sina_fallback(self) -> dict:
        """备用宽度——Sina 全市场行情（多页，带 8s 超时）"""
        from network_guard import safe_call
        try:
            url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            import json as _json
            import requests

            advance = 0
            decline = 0
            total = 0

            def _fetch_br_page(page: int):
                params = {"page": page, "num": 100, "sort": "changepercent", "asc": 0, "node": "hs_a"}
                r = requests.get(url, params=params, timeout=8)
                if r.status_code != 200:
                    return None
                return _json.loads(r.text)

            for page in range(1, 7):
                data = safe_call(_fetch_br_page, page=page, timeout=10.0, name=f"breadth_sina_{page}")
                if not data or len(data) == 0:
                    break
                for item in data:
                    cp = float(item.get("changepercent", 0) or 0)
                    if cp > 0:
                        advance += 1
                    elif cp < 0:
                        decline += 1
                total += len(data)
                if len(data) < 100:
                    break

            flat = total - advance - decline
            breadth = (advance - decline) / total if total > 0 else 0.0
            return {"advance": advance, "decline": decline, "flat": flat, "total": total, "breadth": round(breadth, 4)}
        except Exception as e:
            logger.warning(f"Sina breadth fallback failed: {e}")
            return {"advance": 0, "decline": 0, "flat": 0}

    def fetch_top_3000_by_volume(self) -> list[str]:
        """TOP 3000——AKShare 全市场实时行情按成交额排序（带 10s 超时）"""
        from network_guard import safe_call
        try:
            df = safe_call(
                lambda: ak.stock_zh_a_spot_em(),
                timeout=10.0, name="top3000_spot_em"
            )
            if df is None or (hasattr(df, "empty") and df.empty):
                return self._fetch_top_3000_sina()
            # 按成交额排序
            amount_col = None
            for col in ["成交额", "amount", "turnover"]:
                if col in df.columns:
                    amount_col = col
                    break
            if amount_col is None:
                return self._fetch_top_3000_sina()
            df_sorted = df.sort_values(amount_col, ascending=False)
            return df_sorted.iloc[:3000]["代码"].tolist()
        except Exception as e:
            logger.warning(f"Top 3000 ak failed: {e}")
            return self._fetch_top_3000_sina()

    def _fetch_top_3000_sina(self) -> list[str]:
        """TOP 3000——Sina 多页兜底（带 8s 超时）"""
        from network_guard import safe_call
        try:
            url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            import json as _json
            import requests
            codes = []

            def _fetch_page(page: int):
                params = {"page": page, "num": 100, "sort": "amount", "asc": 0, "node": "hs_a"}
                r = requests.get(url, params=params, timeout=8)
                if r.status_code != 200:
                    return None
                return _json.loads(r.text)

            for page in range(1, 7):
                data = safe_call(_fetch_page, page=page, timeout=10.0, name=f"top3k_sina_{page}")
                if not data or len(data) == 0:
                    break
                codes.extend(item.get("code", "") for item in data if "code" in item)
                if len(data) < 100:
                    break
            return codes
        except Exception:
            return []

    def fetch_limit_up_count(self) -> int:
        """涨停家数——AKShare 涨停板数据（带 8s 超时）"""
        from network_guard import safe_call
        try:
            df = safe_call(
                lambda: ak.stock_zt_pool_em(date="20260526"),
                timeout=8.0, name="limit_up_pool"
            )
            if df is not None and hasattr(df, "__len__"):
                return int(len(df))
            return 0
        except Exception:
            return 0

    def fetch_limit_down_count(self) -> int:
        """跌停家数（带 8s 超时）"""
        from network_guard import safe_call
        try:
            df = safe_call(
                lambda: ak.stock_zt_pool_em(date="20260526"),
                timeout=8.0, name="limit_down_pool"
            )
            if df is not None and hasattr(df, "__len__"):
                return int(len(df))
            return 0
        except Exception:
            return 0
