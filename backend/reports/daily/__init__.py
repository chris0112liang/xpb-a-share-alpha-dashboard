"""
reports/daily/__init__.py — Daily Alpha Report Generator

盘后运行，输出 daily report JSON 到 reports/daily/YYYYMMDD.json。
"""

from __future__ import annotations

from .report import generate_daily_report
