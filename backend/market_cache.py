"""
market_cache.py — 共享磁盘缓存，应对休市日 akshare 请求返回空数据的问题。
拉数成功时保存到磁盘；拉数失败时从磁盘加载最近交易日缓存。

缓存文件格式（JSON）:
  {"cached_at": <unixtime>, "trade_date": "YYYY-MM-DD", "data": <payload>}
"""

from __future__ import annotations
import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger("alpha.market_cache")

_MARKET_CACHE_DIR = Path("/tmp") / "xpb_cache"

def _ensure_cache_dir() -> None:
    _MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _infer_trade_date() -> str:
    """推断最近一个交易日（周一到周五）"""
    today = datetime.now()
    wd = today.weekday()  # 0=Mon .. 6=Sun
    if wd == 5:
        today = today - timedelta(days=1)   # Sat → Fri
    elif wd == 6:
        today = today - timedelta(days=2)   # Sun → Fri
    return today.strftime("%Y-%m-%d")

def _extract_trade_date(payload) -> str:
    """优先从 payload 中提取真实行情日期，提取不到再按日历推断。"""
    if isinstance(payload, list):
        dates = sorted({
            str(item.get("date", ""))[:10]
            for item in payload
            if isinstance(item, dict) and item.get("date")
        })
        if dates:
            return dates[-1]
        return _infer_trade_date()
    if not isinstance(payload, dict):
        return _infer_trade_date()
    direct = payload.get("trade_date") or payload.get("date")
    if direct:
        return str(direct)[:10]
    quotes = payload.get("quotes")
    if isinstance(quotes, list):
        dates = sorted({str(q.get("date", ""))[:10] for q in quotes if isinstance(q, dict) and q.get("date")})
        if dates:
            return dates[-1]
    return _infer_trade_date()

def save_json_cache(filename: str, payload) -> None:
    """保存 payload 到磁盘缓存"""
    _ensure_cache_dir()
    envelope = {
        "cached_at": time.time(),
        "trade_date": _extract_trade_date(payload),
        "data": payload,
    }
    try:
        fp = _MARKET_CACHE_DIR / filename
        fp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[MarketCache] 已保存: {filename}")
    except Exception as e:
        logger.warning(f"[MarketCache] 保存失败 {filename}: {e}")

def load_json_cache(filename: str, max_age_hours: float = 48) -> dict | None:
    """加载磁盘缓存，过期返回 None。返回内层 data 字典。"""
    fp = _MARKET_CACHE_DIR / filename
    if not fp.exists():
        return None
    try:
        envelope = json.loads(fp.read_text(encoding="utf-8"))
        cached_at = envelope.get("cached_at", 0)
        age_hours = (time.time() - cached_at) / 3600
        if age_hours > max_age_hours:
            logger.info(f"[MarketCache] {filename} 过期 ({age_hours:.1f}h > {max_age_hours}h)")
            return None
        logger.info(f"[MarketCache] 加载 {filename}: 缓存龄 {age_hours:.1f}h, 交易日 {envelope.get('trade_date', '?')}")
        return envelope.get("data")
    except Exception as e:
        logger.warning(f"[MarketCache] 读取失败 {filename}: {e}")
        return None

def load_cache_metadata(filename: str) -> dict:
    """加载缓存元数据（cached_at, trade_date），不返回 data"""
    fp = _MARKET_CACHE_DIR / filename
    if not fp.exists():
        return {}
    try:
        envelope = json.loads(fp.read_text(encoding="utf-8"))
        return {
            "cached_at": envelope.get("cached_at"),
            "trade_date": envelope.get("trade_date", ""),
        }
    except Exception:
        return {}
