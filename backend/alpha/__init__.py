"""
alpha — AI Alpha 个股扫描系统

策略驱动的自动筛选链路：
  WorldState → StrategySelector → AlphaScanner → CandidateStocks → Ranking

不依赖 LLM，纯结构化规则引擎。
"""

from alpha.scanner import scan
from alpha.ranking import build_report
from alpha.market_filter import fetch_market_snapshot

__all__ = ["scan", "build_report", "fetch_market_snapshot"]
