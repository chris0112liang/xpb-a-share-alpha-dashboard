"""
reports/scheduler.py — 运行调度器（非后台线程，纯 async 轮询）

API 端点触发：
  - POST /api/replay/report/record  → 记录一次快照 → 返回 snapshot_id
  - POST /api/replay/report/daily   → 生成当日报告

盘后手动：
  - POST /api/replay/validate?prev_date=YYYYMMDD
  - POST /api/replay/daily-report

每轮 checkpoint，用户决定是否继续投入后端开发。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 这个文件仅作为调度逻辑的文档入口。
# 所有 API 调用已在 replay/api.py 中实现。
# 调度通过用户 cron 或手动调用完成。
