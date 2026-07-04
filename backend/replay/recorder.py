"""
replay/recorder.py — 快照记录器

每个交易时段周期性：
  1. 调用 AlphaBrain.tick() 获取完整快照
  2. 记录到 DuckDB（或 Parquet）

数据格式：
  - snapshot_id: {date}-{seq:02d}
  - ts: datetime
  - data: JSON

验证用数据：
  - regime / rotation_speed / risk_level
  - active_strategies
  - top_candidates (code, score, tier, risk_reward, reasons)
  - market_events
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 存储路径 ──
REPLAY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "replay")


def _ensure_dir():
    os.makedirs(REPLAY_DIR, exist_ok=True)


def _daily_path(date_str: str = "") -> str:
    """按日期存储一个快照文件"""
    _ensure_dir()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(REPLAY_DIR, f"snapshots_{date_str}.jsonl")


def record_snapshot(snapshot: dict) -> str:
    """记录一个快照到当日日志文件

    snapshot: AlphaBrain.tick() 返回的完整快照 dict
    返回: snapshot_id
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")

    # 按天计数
    path = _daily_path(date_str)
    seq = 0
    if os.path.exists(path):
        with open(path) as f:
            for _ in f:
                seq += 1

    snapshot_id = f"{date_str}-{seq + 1:02d}"

    record = {
        "snapshot_id": snapshot_id,
        "ts": now.isoformat(),
        "date": date_str,
        "seq": seq + 1,
        "regime": snapshot.get("regime", ""),
        "regime_display": snapshot.get("regime_display", ""),
        "primary_strategy": snapshot.get("primary_strategy", ""),
        "active_strategies": snapshot.get("active_strategies", []),
        "rotation_speed": snapshot.get("rotation_speed", 0.0),
        "risk_level": snapshot.get("risk_level", 0.0),
        "market_bias": snapshot.get("market_bias", 0.0),
        "leading_sectors": snapshot.get("leading_sectors", []),
        "candidates": [
            {
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "score": c.get("score"),
                "tier": c.get("tier"),
                "confidence": c.get("confidence"),
                "risk_reward": c.get("risk_reward"),
                "data_source": c.get("data_source", "fallback"),
                "relative_strength": c.get("relative_strength", 0.0),
                "atr_pct": c.get("atr_pct", 0.0),
                "change_pct_5d": c.get("change_pct_5d", 0.0),
            }
            for c in snapshot.get("top_candidates", [])
        ],
        "events": [
            {
                "event_type": e.get("event_type"),
                "severity": e.get("severity"),
                "description": e.get("description"),
            }
            for e in snapshot.get("market_events", [])
        ],
        "ai_summary": snapshot.get("ai_summary", ""),
        "world_state_summary": {
            "regime": snapshot.get("world_state", {}).get("regime", ""),
            "rotation_speed": snapshot.get("world_state", {}).get("rotation_speed", 0.0),
            "risk_level": snapshot.get("world_state", {}).get("risk_level", 0.0),
            "breadth_score": snapshot.get("world_state", {}).get("breadth_score", 0.0),
            "momentum_score": snapshot.get("world_state", {}).get("momentum_score", 0.0),
            "leading_sectors": snapshot.get("world_state", {}).get("leading_sectors", []),
        },
    }

    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    logger.info(f"[Replay] recorded {snapshot_id} ({len(snapshot.get('top_candidates',[]))} candidates)")
    return snapshot_id


def list_snapshots(date_str: str | None = None, limit: int = 20) -> list[dict]:
    """列出某天的已记录快照"""
    path = _daily_path(date_str or "")
    if not os.path.exists(path):
        return []

    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    return records[-limit:]


def load_snapshot(snapshot_id: str) -> Optional[dict]:
    """按 snapshot_id 加载一个快照"""
    date_str = snapshot_id.split("-")[0]
    records = list_snapshots(date_str, limit=500)
    for r in records:
        if r.get("snapshot_id") == snapshot_id:
            return r
    return None


def load_latest_snapshot(date_str: str | None = None) -> Optional[dict]:
    """加载最新一条快照"""
    records = list_snapshots(date_str, limit=1)
    if records:
        # list_snapshots 返回末尾的记录（最新在最后），所以取 -1
        return records[-1] if len(records) > 0 else records[0]
    return None
