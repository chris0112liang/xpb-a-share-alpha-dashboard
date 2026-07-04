"""
alpha_os/terminal.py — Alpha 终端（API 层桥接）

职责：
  terminal() 函数组装前端需要的 /api/alpha/terminal 响应。
  实际上是 AlphaBrain.tick() 的轻量包装。

以后前端：
  不再分别请求 /world /regime /strategies /candidates
  只请求 GET /api/alpha/terminal → AlphaSnapshot
"""

from __future__ import annotations

from .brain import AlphaBrain


def terminal() -> dict:
    """前端 API 入口

    返回完整的 AlphaSnapshot（dict），包含：
    - ts / world_state / regime info
    - active_strategies + leading_sectors
    - turning_alerts + top_candidates
    - ai_summary + ai_explanation + market_events
    """
    return AlphaBrain.tick()
