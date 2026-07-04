"""
replay/api.py — Replay 系统的 API 端点

注册到 main.py 的路由：
  GET/POST /api/replay/record   — 记录当前快照
  GET     /api/replay/snapshots — 列出指定日期的快照
  GET     /api/replay/snapshot  — 获取指定 snapshot_id
  POST    /api/replay/validate  — 运行验证
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from .recorder import record_snapshot, list_snapshots, load_snapshot, load_latest_snapshot
from .validator import run_daily_validation
from alpha.calibration import calibration_report
from reports.daily import generate_daily_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/replay")


@router.post("/record")
async def api_record():
    """记录当前 AlphaBrain 快照"""
    from alpha_os.brain import AlphaBrain
    snapshot = AlphaBrain.tick()
    sid = record_snapshot(snapshot)
    return {"status": "ok", "snapshot_id": sid, "ts": snapshot.get("ts", "")}


@router.get("/snapshots")
async def api_list_snapshots(
    date: str = Query(default="", description="日期 YYYYMMDD"),
    limit: int = Query(default=20, ge=1, le=200),
):
    """列出快照"""
    records = list_snapshots(date if date else None, limit=limit)
    return {"snapshots": records, "count": len(records)}


@router.get("/snapshot")
async def api_get_snapshot(
    snapshot_id: str = Query(..., description="snapshot_id, e.g. 20260526-01"),
):
    """获取指定快照"""
    record = load_snapshot(snapshot_id)
    if record:
        return {"snapshot": record}
    return {"error": "not found"}, 404


@router.post("/validate")
async def api_validate(
    prev_date: str = Query(..., description="验证日期 YYYYMMDD"),
):
    """运行验证（需要当天数据到位时调用）"""
    # 构建占位数据
    today = datetime.now()
    today_state = {
        "regime": "",
        "rotation_speed": 0.0,
    }

    result = run_daily_validation(
        prev_date, today_state, {}, {}, {}, {}
    )
    return result


@router.get("/calibration")
async def api_calibration(
    days: int = Query(default=7, ge=1, le=90),
):
    """生成校准报告"""
    return calibration_report(days=days)


@router.post("/daily-report")
async def api_daily_report(
    prev_date: str = Query(default="", description="昨日日期 YYYYMMDD"),
):
    """生成当日 Alpha Daily Report"""
    from alpha_os.brain import AlphaBrain
    snapshot = AlphaBrain.tick()
    pd = prev_date if prev_date else None
    report = generate_daily_report(alpha_snapshot=snapshot, prev_date=pd)
    return report


@router.get("/daily-report")
async def api_get_daily_report(
    date: str = Query(default="", description="日期 YYYYMMDD"),
):
    """读取已有的 Daily Report"""
    import json, os
    date_str = date or datetime.now().strftime("%Y%m%d")
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "daily", f"{date_str}.json")
    if not os.path.exists(path):
        return {"error": f"no report for {date_str}"}, 404
    with open(path) as f:
        return json.load(f)
