"""
alpha/market_filter.py — 全市场行情预处理过滤器

从 AKShare 实时行情获取全市场数据，做一次粗筛：
1. 排除 ST/退市/北交所
2. 按成交额取活跃标的（全A TOP 3000量级）
3. 返回标准化字典列表

不在此做个股判定——只做"什么值得看"的粗筛选。

★ 自带持久化缓存：API可用时刷新缓存，API断掉时自动回退到最近一次成功快照
★ 即使东方财富/Sina/腾讯全部不可用，scanner也能基于缓存数据正常工作
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 持久化行情缓存 ──
_MARKET_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "market"
_MARKET_CACHE_FILE = _MARKET_CACHE_DIR / "market_snapshot_cache.json"
_CACHE_TTL = 7200  # 2小时有效，过期仍然可用（降级标记）


def _load_market_cache() -> tuple[list[dict], bool]:
    """加载磁盘缓存的行情快照。
    返回：(stocks, is_from_cache)
    """
    if not _MARKET_CACHE_FILE.exists():
        return [], False
    try:
        data = json.loads(_MARKET_CACHE_FILE.read_text(encoding="utf-8"))
        cached_ts = data.get("cached_at", 0)
        cached_stocks = data.get("stocks", [])
        cache_age = time.time() - cached_ts
        is_fresh = cache_age < _CACHE_TTL
        trade_ready = sum(
            1 for s in cached_stocks
            if float(s.get("price", 0) or 0) > 0 and float(s.get("amount", 0) or 0) > 0
        )
        if trade_ready == 0 and cached_stocks:
            for s in cached_stocks:
                s["is_fallback"] = True
                s["data_source"] = "fallback_cache"
            logger.warning(
                "[MarketCache] 缓存没有可交易行情字段，仅作为股票池/板块映射使用"
            )
        # 按综合价值排序选最优缓存：有板块归属且数量多 > 无板块数量多
        with_sector = sum(1 for s in cached_stocks if s.get("sector", ""))
        logger.info(f"[MarketCache] 加载缓存: {len(cached_stocks)} 只({with_sector}只有板块), "
                    f"缓存龄 {cache_age:.0f}s, 可交易 {trade_ready} 只, fresh={is_fresh}")
        return cached_stocks, True
    except Exception as e:
        logger.warning(f"[MarketCache] 缓存读取失败: {e}")
        return [], False


def _save_market_cache(stocks: list[dict]):
    """将成功的行情快照写入磁盘缓存。

    仅当新数据质量（板块覆盖度）不低于现有缓存时才覆写。
    """
    if not stocks:
        return
    try:
        _MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # 只保存关键字段精简缓存体积
        new_with_sector = sum(1 for s in stocks[:2000] if s.get("sector", ""))

        # 检查现有缓存质量
        existing_keep = True
        if _MARKET_CACHE_FILE.exists():
            try:
                existing = json.loads(_MARKET_CACHE_FILE.read_text(encoding="utf-8"))
                existing_sector = sum(1 for s in existing.get("stocks", []) if s.get("sector", ""))
                existing_count = existing.get("count", 0)
                existing_source = existing.get("data_source", "")
                # 如果现有缓存是板块池（全有板块）且数量多有板块 > 新数据，保留
                if existing_source == "builtin_sectors" and existing_sector >= new_with_sector:
                    logger.info(f"[MarketCache] 跳过缓存覆写: 现有缓存 {existing_count}只({existing_sector}板块) >= 新数据 {new_with_sector}板块")
                    return
                # Sina数据（无板块）不要覆写有板块的缓存
                if new_with_sector == 0 and existing_sector > 0:
                    logger.info(f"[MarketCache] 跳过缓存覆写: 新数据无板块归属，保留现有缓存")
                    return
                existing_keep = False
            except Exception:
                existing_keep = False

        slim = []
        for s in stocks[:2000]:
            slim.append({
                "code": s.get("code", ""),
                "name": s.get("name", ""),
                "price": s.get("price", 0.0),
                "change_pct": s.get("change_pct", 0.0),
                "volume": s.get("volume", 0.0),
                "amount": s.get("amount", 0.0),
                "turnover": s.get("turnover", 0.0),
                "sector": s.get("sector", ""),
                "is_fallback": bool(s.get("is_fallback", False)),
                "data_source": "cache" if not s.get("is_fallback", False) else "fallback_cache",
            })
        payload = {
            "cached_at": time.time(),
            "count": len(slim),
            "data_source": "live_api",
            "stocks": slim,
        }
        _MARKET_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[MarketCache] 缓存已保存: {len(slim)} 只")
    except Exception as e:
        logger.warning(f"[MarketCache] 缓存写入失败: {e}")


# ST / 退市 / 北交所 过滤
_STOCK_NAME_BLACKLIST = re.compile(r"ST|退|N[^A-Z]|新股|次新", re.IGNORECASE)
_BAD_CODE_PREFIXES = ("8", "4", "3")  # 北交所 8xxxxx / 三板 4xxxxx / 未用


def is_valid_stock(code: str, name: str = "") -> bool:
    """粗筛：是否值得分析"""
    if code.startswith(_BAD_CODE_PREFIXES):
        return False
    if _STOCK_NAME_BLACKLIST.search(name):
        return False
    return True


def fetch_market_snapshot(max_stocks: int = 2000) -> list[dict]:
    """
    获取全市场实时快照（真实 AKShare API）

    ★ 绕过 safe_call 的全局熔断器，使用独立 ThreadPoolExecutor。
    ★ 成功时自动持久化到磁盘缓存；失败时自动回退到最近一次成功的缓存数据。
    """
    import concurrent.futures as _cf

    # ── 方案 A: AKShare EM spot（直接调用，不走熔断器） ──
    df = None
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(__import__("akshare").stock_zh_a_spot_em)
            df = _fut.result(timeout=12.0)
    except Exception:
        pass

    if df is None or (hasattr(df, "empty") and df.empty):
        # 重试一次
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(__import__("akshare").stock_zh_a_spot_em)
                df = _fut.result(timeout=10.0)
        except Exception:
            pass

    if df is None or (hasattr(df, "empty") and df.empty):
        logger.warning("[MarketFilter] EM spot failed, fallback to Sina")
        sina_result = _fetch_market_snapshot_sina(max_stocks)
        # Sina 返回的是内置池假数据时（price=0），不要覆写缓存，回退到缓存
        if sina_result and sina_result[0].get("price", 0) > 0:
            _save_market_cache(sina_result)
            # 如果 Sina 数据不足（<200只或板块归属太少），回退缓存
            cached, _ = _load_market_cache()
            if cached and len(cached) > len(sina_result):
                logger.info(f"[MarketFilter] Sina {len(sina_result)}只 < 缓存{len(cached)}只，使用缓存")
                return cached[:max_stocks]
            return sina_result
        # Sina 也失败 → 回退磁盘缓存
        cached, _ = _load_market_cache()
        if cached:
            logger.info(f"[MarketFilter] 回退到缓存快照: {len(cached)} 只")
            return cached[:max_stocks]
        return sina_result

    # 列名映射（AKShare 中文→英文）
    col_map = {
        "代码": "code", "名称": "name", "最新价": "price",
        "涨跌幅": "change_pct", "涨跌额": "change",
        "成交量": "volume", "成交额": "amount", "换手率": "turnover",
    }
    for alt_col, eng_col in [
        ("pctChg", "change_pct"), ("changepercent", "change_pct"),
        ("amount", "amount"), ("turnover", "turnover"),
    ]:
        if alt_col in df.columns and eng_col not in col_map.values():
            col_map[alt_col] = eng_col

    stocks = []
    for _, row in df.iterrows():
        code = str(row.get("代码", ""))
        name = str(row.get("名称", ""))
        if not code or not name:
            continue
        if not is_valid_stock(code, name):
            continue

        amount = _get_col_val(row, "amount", 0.0, ["成交额", "amount", "turnover"])
        price = _get_col_val(row, "price", 0.0, ["最新价", "price", "lastPrice"])
        change_pct = _get_col_val(row, "change_pct", 0.0, ["涨跌幅", "pctChg", "changepercent"])
        turnover = _get_col_val(row, "turnover", 0.0, ["换手率", "turnover", "turnoverrate"])

        stocks.append({
            "code": code,
            "name": name,
            "price": price,
            "change_pct": change_pct,
            "volume": _get_col_val(row, "volume", 0.0, ["成交量", "volume"]),
            "amount": amount,
            "turnover": turnover,
            "is_fallback": False,
            "data_source": "live",
        })

    stocks.sort(key=lambda s: s["amount"], reverse=True)
    _save_market_cache(stocks)
    logger.info(f"[MarketFilter] EM snapshot: {len(stocks)} stocks")
    return stocks[:max_stocks]


def _fetch_market_snapshot_sina(max_stocks: int) -> list[dict]:
    """Sina API 全市场行情兜底（直接调用，不走熔断器）"""
    import concurrent.futures as _cf

    def _fetch_one_page(page: int) -> Optional[list]:
        try:
            import json as _json
            import requests
            url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            params = {"page": page, "num": 100, "sort": "amount", "asc": 0, "node": "hs_a"}
            r = requests.get(url, params=params, timeout=8)
            if r.status_code != 200:
                return None
            return _json.loads(r.text)
        except Exception:
            return None

    stocks = []
    for page in range(1, 7):
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(_fetch_one_page, page)
                data = _fut.result(timeout=10.0)
        except Exception:
            data = None
        if not data:
            break
        for item in data:
            code = item.get("code", "")
            name = item.get("name", "")
            if not code or not name or not is_valid_stock(code, name):
                continue
            stocks.append({
                "code": code,
                "name": name,
                "price": float(item.get("trade", 0) or 0),
                "change_pct": float(item.get("changepercent", 0) or 0),
                "volume": float(item.get("volume", 0) or 0),
                "amount": float(item.get("amount", 0) or 0),
                "turnover": float(item.get("turnover", 0) or 0),
                "is_fallback": False,
                "data_source": "live",
            })
        if len(data) < 100:
            break
    if stocks:
        stocks.sort(key=lambda s: s["amount"], reverse=True)
        _save_market_cache(stocks)
        logger.info(f"[MarketFilter] Sina snapshot: {len(stocks)} stocks")
        return stocks[:max_stocks]
    # 全部失败，用内置池
    logger.warning("[MarketFilter] All external APIs failed, using built-in stock pool")
    return _fetch_market_snapshot_builtin(max_stocks)


def _fetch_market_snapshot_builtin(max_stocks: int = 2000) -> list[dict]:
    """当所有外部API不可用时，使用内置板块成分股构建股票列表。

    从 sector_scanner.BUILTIN_SECTORS 读取板块成分股，
    构造含板块归属的股票列表，标记为 fallback 数据源。
    不做任何伪数据生成——所有数值字段返回 0。
    """
    # 合并所有板块成分股
    stocks = []
    seen = set()
    try:
        from sector_scanner import BUILTIN_SECTORS
        for sector_name, sector_info in BUILTIN_SECTORS.items():
            sector_stocks = sector_info.get("stocks", []) if isinstance(sector_info, dict) else []
            for code in sector_stocks:
                if code in seen:
                    continue
                seen.add(code)
                if not is_valid_stock(code, ""):
                    continue
                stocks.append({
                    "code": code,
                    "name": "",  # builtin sectors only store codes, names filled by scanner
                    "price": 0.0,
                    "change_pct": 0.0,
                    "volume": 0.0,
                    "amount": 0.0,
                    "turnover": 0.0,
                    "sector": sector_name,
                    "is_fallback": True,
                    "data_source": "fallback",
                })
    except Exception:
        pass

    # 如果板块成分股也为空，用经典蓝筹池兜底
    if not stocks:
        _SMALL_POOL = [
            ("600519", "贵州茅台"), ("300750", "宁德时代"), ("000858", "五粮液"),
            ("601318", "中国平安"), ("600036", "招商银行"), ("000333", "美的集团"),
            ("600900", "长江电力"), ("002594", "比亚迪"), ("300059", "东方财富"),
            ("601166", "兴业银行"), ("600276", "恒瑞医药"), ("688981", "中芯国际"),
            ("600887", "伊利股份"), ("002230", "科大讯飞"), ("000568", "泸州老窖"),
            ("601288", "农业银行"), ("601398", "工商银行"), ("600000", "浦发银行"),
            ("000651", "格力电器"), ("000100", "TCL科技"),
        ]
        for code, name in _SMALL_POOL:
            stocks.append({
                "code": code, "name": name,
                "price": 0.0, "change_pct": 0.0,
                "volume": 0.0, "amount": 0.0, "turnover": 0.0,
                "sector": "", "is_fallback": True, "data_source": "fallback",
            })
        seen = {s["code"] for s in stocks}

    logger.info(f"[MarketFilter] Builtin pool: {len(stocks)} unique stocks from {len(seen)} codes")
    return stocks[:max_stocks]


def _get_col_val(row, eng_key: str, default: float, alternatives: list[str]) -> float:
    """兼容不同列名的取值函数"""
    # 精确匹配
    for col in [eng_key] + alternatives:
        if col in row.index:
            val = row[col]
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default
    return default
