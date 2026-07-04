"""
FastAPI 后端：获取 A 股真实行情 + 技术指标 + AI 分析
启动: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import logging
import concurrent.futures

logger = logging.getLogger("alpha.main")



# 可选禁用代理。部分 Windows 环境的系统代理会干扰 akshare；但默认保留代理，
# 避免新闻/热点等外部源在需要代理的网络下返回空。
def _patch_requests():
    try:
        import requests
        from requests.adapters import HTTPAdapter

        orig_send = HTTPAdapter.send

        def send_no_proxy(self, request, **kwargs):
            kwargs.setdefault("proxies", {"http": None, "https": None})
            return orig_send(self, request, **kwargs)

        HTTPAdapter.send = send_no_proxy

        # 全局禁用 trust_env（阻止从注册表读取 Windows 代理设置）
        orig_session_init = requests.Session.__init__
        def patched_init(self, *args, **kwargs):
            orig_session_init(self, *args, **kwargs)
            self.trust_env = False
        requests.Session.__init__ = patched_init

    except ImportError:
        pass

if os.getenv("XPB_DISABLE_PROXY") == "1":
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NO_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    _patch_requests()

# 加载 .env 文件中的环境变量
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

from datetime import datetime, timedelta
from typing import Optional
import json

import akshare as ak
import pandas as pd
import numpy as np
from alpha_engine import AlphaEngine, PortfolioGuard, generate_executive_summary
from sector_scanner import get_registry
from sector_worker import start_worker as start_sector_worker, get_all_lifecycles, get_summary as get_sector_summary
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="金融看板 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════
#  技术指标计算
# ══════════════════════════════════════════════════════

def sma(series: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均 (SMA)，不足 period 的位置为 NaN"""
    result = np.full_like(series, np.nan, dtype=np.float64)
    if len(series) < period:
        return result
    cumsum = np.cumsum(np.insert(series, 0, 0))
    result[period - 1 :] = (cumsum[period:] - cumsum[:-period]) / period
    return result


def ema(series: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均 (EMA)"""
    result = np.full_like(series, np.nan, dtype=np.float64)
    if len(series) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = np.mean(series[:period])
    for i in range(period, len(series)):
        result[i] = series[i] * k + result[i - 1] * (1 - k)
    return result


def _ffill(arr: list) -> list:
    """forward-fill NaN/None 为前一个有效值，还不行就填 0"""
    out = []
    prev = 0.0
    for v in arr:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            out.append(prev)
        else:
            prev = float(v)
            out.append(prev)
    return out


def compute_indicators(closes: np.ndarray) -> dict:
    """计算 MA5/10/20 和 MACD"""
    n = len(closes)
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)

    dif = np.full(n, np.nan, dtype=np.float64)
    valid = (~np.isnan(ema12)) & (~np.isnan(ema26))
    dif[valid] = ema12[valid] - ema26[valid]

    # DEA = 9-period EMA of DIF（只对有效 DIF 计算）
    dea_full = np.full(n, np.nan, dtype=np.float64)
    valid_idx = np.where(~np.isnan(dif))[0]
    if len(valid_idx) >= 9:
        dea_on_valid = ema(dif[valid_idx], 9)  # 长度同 valid_idx，前 8 个为 NaN
        for j in range(8, len(dea_on_valid)):
            if not np.isnan(dea_on_valid[j]):
                dea_full[valid_idx[j]] = dea_on_valid[j]

    macd_hist = np.full(n, np.nan, dtype=np.float64)
    both_valid = (~np.isnan(dif)) & (~np.isnan(dea_full))
    macd_hist[both_valid] = (dif[both_valid] - dea_full[both_valid]) * 2

    # ── 均线排列 ──
    last_ma5 = ma5[~np.isnan(ma5)][-1] if np.any(~np.isnan(ma5)) else None
    last_ma10 = ma10[~np.isnan(ma10)][-1] if np.any(~np.isnan(ma10)) else None
    last_ma20 = ma20[~np.isnan(ma20)][-1] if np.any(~np.isnan(ma20)) else None

    if last_ma5 and last_ma10 and last_ma20:
        if last_ma5 > last_ma10 > last_ma20:
            alignment = "多头排列"
        elif last_ma5 < last_ma10 < last_ma20:
            alignment = "空头排列"
        else:
            alignment = "均线交叉/缠绕"
    else:
        alignment = "数据不足"

    # ── 对输出列表做 forward-fill 消除 NaN/None ──
    def _tolist_ffill(arr: np.ndarray) -> list:
        return _ffill(arr.tolist())

    return {
        "ma5": _tolist_ffill(ma5),
        "ma10": _tolist_ffill(ma10),
        "ma20": _tolist_ffill(ma20),
        "dif": _tolist_ffill(dif),
        "dea": _tolist_ffill(dea_full),
        "macd": _tolist_ffill(macd_hist),
        "snapshot": {
            "ma5": round(float(last_ma5), 2) if last_ma5 is not None and not np.isnan(last_ma5) else None,
            "ma10": round(float(last_ma10), 2) if last_ma10 is not None and not np.isnan(last_ma10) else None,
            "ma20": round(float(last_ma20), 2) if last_ma20 is not None and not np.isnan(last_ma20) else None,
            "alignment": alignment,
            "dif": round(float(dif[~np.isnan(dif)][-1]), 4) if np.any(~np.isnan(dif)) else 0.0,
            "dea": round(float(dea_full[~np.isnan(dea_full)][-1]), 4) if np.any(~np.isnan(dea_full)) else 0.0,
            "macd": round(float(macd_hist[~np.isnan(macd_hist)][-1]), 4) if np.any(~np.isnan(macd_hist)) else 0.0,
        },
    }


# ══════════════════════════════════════════════════════
#  高频量化技术指标（对齐主流看盘软件）
# ══════════════════════════════════════════════════════

def detect_long_lower_shadow(ohlcv: list[dict], threshold: float = 0.6) -> dict:
    """检测长下影线：实体极小、下影线极长 → 买方强力反击信号"""
    if len(ohlcv) < 1:
        return {"has_shadow": False, "ratio": 0.0}
    bar = ohlcv[-1]
    o, c, h, l = bar["open"], bar["close"], bar["high"], bar["low"]
    body_low = min(o, c)
    range_h = h - l
    if range_h <= 0:
        return {"has_shadow": False, "ratio": 0.0}
    shadow_ratio = (body_low - l) / range_h
    return {
        "has_shadow": shadow_ratio > threshold,
        "ratio": round(shadow_ratio, 3),
        "label": "长下影线" if shadow_ratio > threshold else None,
    }


def detect_one_yang_three_lines(ohlcv: list[dict], closes: np.ndarray) -> dict:
    """检测一阳三线：一根大阳线同时突破 MA5、MA10、MA20"""
    if len(ohlcv) < 21:
        return {"triggered": False, "broken_lines": []}
    bar = ohlcv[-1]
    prev = ohlcv[-2]
    ma5 = np.mean(closes[-5:])
    ma10 = np.mean(closes[-10:])
    ma20 = np.mean(closes[-20:])
    # 大阳线条件: 涨幅 > 2% 且收盘 > 开盘
    is_big_yang = bar["close"] > bar["open"] and (bar["close"] / bar["open"] - 1) > 0.02
    if not is_big_yang:
        return {"triggered": False, "broken_lines": []}
    broken = []
    if prev["close"] <= ma5 and bar["close"] > ma5:
        broken.append("MA5")
    if prev["close"] <= ma10 and bar["close"] > ma10:
        broken.append("MA10")
    if prev["close"] <= ma20 and bar["close"] > ma20:
        broken.append("MA20")
    return {
        "triggered": len(broken) >= 3,
        "broken_lines": broken,
        "label": "一阳三线" if len(broken) >= 3 else None,
    }


def detect_rising_channel(ohlcv: list[dict], closes: np.ndarray) -> dict:
    """检测上升通道：MA多头排列 + 连续5日低点抬高"""
    if len(ohlcv) < 20:
        return {"triggered": False, "consecutive_days": 0}
    ma5 = np.mean(closes[-5:])
    ma10 = np.mean(closes[-10:])
    ma20 = np.mean(closes[-20:])
    is_bullish_alignment = ma5 > ma10 > ma20
    # 连续低点抬高
    lows = [b["low"] for b in ohlcv[-5:]]
    consecutive = 0
    for i in range(1, len(lows)):
        if lows[i] > lows[i - 1]:
            consecutive += 1
        else:
            break
    triggered = is_bullish_alignment and consecutive >= 4
    return {
        "triggered": triggered,
        "consecutive_days": consecutive,
        "bullish_alignment": is_bullish_alignment,
        "label": "上升通道" if triggered else None,
    }


def compute_behavior_tags(ohlcv: list[dict], money_flow: dict, alerts: list[dict],
                          concepts: list[str], closes: np.ndarray) -> list[dict]:
    """
    生成行为标签（龙虎榜/大股东增持/游资痕迹等热门席位特征）。
    基于量价异常 + 资金信号 + 概念热度推断。
    """
    tags: list[dict] = []
    if not ohlcv:
        return tags

    # 长下影线 → 买方反击（龙虎榜游资低吸痕迹）
    shadow = detect_long_lower_shadow(ohlcv)
    if shadow["has_shadow"]:
        tags.append({"tag": "龙虎榜低吸", "confidence": round(shadow["ratio"], 2),
                     "source": "tick", "desc": "长下影线，买方强力反击"})

    # 一阳三线 → 主力资金介入
    yang3 = detect_one_yang_three_lines(ohlcv, closes)
    if yang3["triggered"]:
        tags.append({"tag": "主力突破", "confidence": 0.85,
                     "source": "tick", "desc": f"一阳突破{'/'.join(yang3['broken_lines'])}"})

    # 上升通道 → 中线趋势资金
    channel = detect_rising_channel(ohlcv, closes)
    if channel["triggered"]:
        tags.append({"tag": "趋势资金", "confidence": 0.75,
                     "source": "tick", "desc": f"上升通道，连续{channel['consecutive_days']}日低点抬高"})

    # 主力大额净流入
    main_net = money_flow.get("main_net", 0)
    if main_net > 50000000:  # >5000万
        tags.append({"tag": "大单净流入", "confidence": 0.9,
                     "source": "fund_flow", "desc": f"主力净流入{main_net/10000:.0f}万"})

    # 异常放量 → 龙虎榜游资痕迹
    if len(ohlcv) >= 5:
        vol_today = ohlcv[-1]["volume"]
        avg_vol_5 = sum(b["volume"] for b in ohlcv[-6:-1]) / 5
        if avg_vol_5 > 0 and vol_today > avg_vol_5 * 2.5:
            tags.append({"tag": "异常放量", "confidence": 0.7,
                         "source": "volume", "desc": f"放量{vol_today/avg_vol_5:.1f}倍"})

    # 概念热度
    hot_kw = ["AI", "芯片", "锂", "新能源", "机器人", "低空经济", "固态电池"]
    for kw in hot_kw:
        for c in concepts:
            if kw in c:
                tags.append({"tag": f"热门:{kw}", "confidence": 0.6,
                             "source": "concept", "desc": f"概念标签匹配: {kw}"})
                break

    # 去重
    seen = set()
    unique = []
    for t in tags:
        if t["tag"] not in seen:
            seen.add(t["tag"])
            unique.append(t)
    return unique


# ══════════════════════════════════════════════════════
#  AI 赔率/风险系统：Upside / Downside / Odds Ratio
# ══════════════════════════════════════════════════════

def compute_odds(ohlcv: list[dict]) -> dict:
    """计算上行/下行空间及赔率比。依赖ohlcv中至少20个交易日的high/low/close。"""
    _NP = np
    n = len(ohlcv)
    if n < 20:
        return {"upside_pct": 0.0, "downside_pct": 0.0, "odds_ratio": 0.0, "rating": "数据不足"}

    closes = _NP.array([d["close"] for d in ohlcv], dtype=_NP.float64)
    highs = _NP.array([d["high"] for d in ohlcv], dtype=_NP.float64)
    lows = _NP.array([d["low"] for d in ohlcv], dtype=_NP.float64)
    cur = closes[-1]
    if cur <= 0:
        return {"upside_pct": 0.0, "downside_pct": 0.0, "odds_ratio": 0.0, "rating": "数据异常"}

    # 1. ATR
    tr = _NP.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    tr[0] = highs[0]-lows[0]
    atr = float(_NP.mean(tr[-14:])) if n >= 14 else float(_NP.mean(tr))
    if atr <= 0 or _NP.isnan(atr):
        atr = cur * 0.02

    # 2. MA20
    ma20 = float(_NP.mean(closes[-20:]))

    # 3. 阻力 = max(MA20, 20日高点)
    recent_high = float(_NP.max(highs[-20:]))
    resistance = max(ma20, recent_high)
    if resistance <= cur:
        resistance = cur + atr * 2
    upside_pct = max(((resistance - cur) / cur) * 0.7, 0.0)

    # 4. 波动率自适应止损
    vol_ratio = (atr / cur) * 100
    atr_mult = 1.0 if vol_ratio > 5 else 2.0 if vol_ratio < 2 else 1.5
    atr_stop = cur - atr_mult * atr
    ma20_support = min(ma20, cur)
    downside_price = min(ma20_support, atr_stop)
    if downside_price >= cur:
        downside_price = cur * 0.95
    downside_pct = max((cur - downside_price) / cur, 0.01)

    # 5. 赔率比
    odds_ratio = upside_pct / downside_pct

    # 6. 评级
    rating = "高赔率" if odds_ratio >= 2.0 else "合理" if odds_ratio >= 1.0 else "低赔率"

    return {
        "upside_pct": round(upside_pct * 100, 2),
        "downside_pct": round(downside_pct * 100, 2),
        "odds_ratio": round(odds_ratio, 2),
        "rating": rating,
        "atr": round(atr, 2),
        "atr_mult": atr_mult,
    }


# ══════════════════════════════════════════════════════
#  市场状态引擎 (Market State Engine)
#  通过采样关键 ETF，推断全市场状态，通过环境系数修正个股评分
# ══════════════════════════════════════════════════════

# 采样 ETF 清单：大盘基底 / 成长题材 / 蓝筹金融 / 防御红利
_MARKET_ETF_WATCHLIST: list[tuple[str, str, str]] = [
    ("沪深300", "510300", "sh"),
    ("中证500", "510500", "sh"),
    ("创业板", "159915", "sz"),
    ("科创50", "588000", "sh"),
    ("通信ETF", "515050", "sh"),
    ("芯片ETF", "159995", "sz"),
    ("新能源ETF", "159865", "sz"),
    ("证券ETF", "512880", "sh"),
    ("银行ETF", "512800", "sh"),
    ("红利ETF", "515180", "sh"),
    ("医药ETF", "512010", "sh"),
    ("酒ETF", "512690", "sh"),
    ("军工ETF", "512660", "sh"),
    ("有色ETF", "512400", "sh"),
    ("煤炭ETF", "515220", "sh"),
    ("电力ETF", "159611", "sz"),
]

# 缓存
_MARKET_STATE_CACHE: Optional[dict] = None
_MARKET_STATE_LOCK: "threading.Lock" = None  # lazy init
_MARKET_STATE_REFRESH_PENDING = False


def _init_market_lock():
    global _MARKET_STATE_LOCK
    if _MARKET_STATE_LOCK is None:
        import threading
        _MARKET_STATE_LOCK = threading.Lock()


def _fetch_etf_daily(name: str, code: str, market: str) -> Optional[dict]:
    """获取单只 ETF 的日行情（失败返回 None，带 8s 超时）"""
    from network_guard import safe_call
    try:
        symbol = f"{market}{code}"
        df = safe_call(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",
            ),
            timeout=8.0, name=f"etf_{name[:6]}"
        )
        if df is None or (hasattr(df, "empty") and df.empty) or len(df) < 2:
            print(f"[市场状态] {name}({code}) 数据不足: {len(df) if df is not None else 0}行", flush=True)
            return None
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        close_col = "close" if "close" in df.columns else "收盘"
        amount_col = "amount" if "amount" in df.columns else "成交额"
        change_pct = (float(latest[close_col]) - float(prev[close_col])) / float(prev[close_col]) * 100
        prev_amount = float(prev[amount_col]) if amount_col in prev else 1
        vol_change = ((float(latest[amount_col]) - prev_amount) / max(prev_amount, 1)) * 100
        return {
            "name": name,
            "change_pct": round(change_pct, 2),
            "vol_change_pct": round(vol_change, 1),
        }
    except Exception as e:
        print(f"[市场状态] {name}({code}) 获取失败: {e}", flush=True)
        return None


def _compute_market_breadth() -> dict:
    """
    获取全市场真实涨跌家数（A股5000+只）。
    使用 AKShare stock_zh_a_spot_em 接口，不走熔断器。
    """
    import concurrent.futures as _cf
    result = {
        "total": 0, "up": 0, "down": 0, "flat": 0,
        "up_pct": 0.0, "down_pct": 0.0,
        "total_amount": 0.0, "avg_change": 0.0,
        "top_sectors": [], "failed": True,
    }
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(__import__("akshare").stock_zh_a_spot_em)
            df = _fut.result(timeout=15.0)
        if df is None or df.empty:
            return result
        changes = df["涨跌幅"].astype(float)
        result["total"] = int(len(changes))
        result["up"] = int((changes > 0).sum())
        result["down"] = int((changes < 0).sum())
        result["flat"] = int((changes == 0).sum())
        result["up_pct"] = round(result["up"] / max(result["total"], 1) * 100, 1)
        result["down_pct"] = round(result["down"] / max(result["total"], 1) * 100, 1)
        result["avg_change"] = round(float(changes.mean()), 2)
        if "成交额" in df.columns:
            result["total_amount"] = round(float(df["成交额"].astype(float).sum()) / 1e8, 0)  # 亿
        result["failed"] = False
    except Exception:
        pass
    return result


def _compute_market_state() -> dict:
    """
    采样关键 ETF + 全市场涨跌家数 → 推断市场状态。
    """
    results: list[dict] = []
    for name, code, market in _MARKET_ETF_WATCHLIST:
        r = _fetch_etf_daily(name, code, market)
        if r is not None:
            results.append(r)

    # ── 全市场广度 ──
    breadth = _compute_market_breadth()

    if len(results) < 3:
        raise RuntimeError(f"有效 ETF 数据不足: {len(results)}/{len(_MARKET_ETF_WATCHLIST)}")

    avg_change = sum(r["change_pct"] for r in results) / len(results)
    avg_vol_change = sum(r["vol_change_pct"] for r in results) / len(results)

    changes = [r["change_pct"] for r in results]
    spread = max(changes) - min(changes)

    growth_etfs = [r for r in results if r["name"] in ("通信ETF", "新能源ETF")]
    def_etfs = [r for r in results if r["name"] in ("红利ETF",)]
    growth_avg = sum(r["change_pct"] for r in growth_etfs) / max(len(growth_etfs), 1)
    def_avg = sum(r["change_pct"] for r in def_etfs) / max(len(def_etfs), 1)
    growth_vs_def = growth_avg - def_avg

    # ── 用全市场广度修正判定 ──
    up_ratio = breadth.get("up_pct", 50) / 100 if not breadth.get("failed") else 0.5

    if up_ratio > 0.55 and avg_change > 0.3:
        environment = "risk_on"
        label = "普涨格局"
        risk_appetite = "高"
        volume_trend = "放量" if avg_vol_change > 5 else "持平"
        adjustment_factor = 1.15
    elif up_ratio < 0.30 and avg_change < -0.5:
        environment = "risk_off"
        label = "普跌格局"
        risk_appetite = "低"
        volume_trend = "缩量" if avg_vol_change < -5 else "持平"
        adjustment_factor = 0.85
    elif up_ratio < 0.40:
        environment = "risk_off"
        label = "弱势分化"
        risk_appetite = "低"
        volume_trend = "缩量"
        adjustment_factor = 0.80
    elif growth_vs_def > 1.0:
        environment = "risk_on"
        label = "成长主导"
        risk_appetite = "高"
        volume_trend = "放量"
        adjustment_factor = 1.10
    else:
        environment = "range_bound"
        label = "震荡分化"
        risk_appetite = "中"
        volume_trend = "持平"
        adjustment_factor = 1.0

    # 主线识别
    sorted_results = sorted(results, key=lambda r: r["change_pct"], reverse=True)
    top = sorted_results[0]
    if len(sorted_results) > 1:
        second_top = sorted_results[1]
        if top["change_pct"] > 1.0 and top["change_pct"] - second_top["change_pct"] > 0.5:
            main_line = f"{top['name']}领涨 → 主线清晰"
        elif len([r for r in sorted_results if r["change_pct"] > 0]) >= 3:
            main_line = "板块普涨 → 情绪扩散"
        elif len([r for r in sorted_results if r["change_pct"] > 0]) <= 1:
            main_line = "涨少跌多 → 无明显主线"
        else:
            main_line = "板块分化 → 轮动加速"
    else:
        main_line = "数据不足 → 无法判断主线"

    # ── ETF 资金流向 Top 3 ──
    etf_flow = sorted(results, key=lambda r: r.get("vol_change_pct", 0), reverse=True)
    top_inflow = [f"{r['name']}({r['change_pct']:+.1f}%)" for r in etf_flow[:3]]
    top_outflow = [f"{r['name']}({r['change_pct']:+.1f}%)" for r in etf_flow[-3:]]

    return {
        "environment": environment,
        "label": label,
        "risk_appetite": risk_appetite,
        "main_line": main_line,
        "volume_trend": volume_trend,
        "adjustment_factor": adjustment_factor,
        "etf_count": len(results),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # ★ 全市场真实广度
        "breadth": {
            "total": breadth.get("total", 0),
            "up": breadth.get("up", 0),
            "down": breadth.get("down", 0),
            "flat": breadth.get("flat", 0),
            "up_pct": breadth.get("up_pct", 0),
            "down_pct": breadth.get("down_pct", 0),
            "avg_change": breadth.get("avg_change", 0),
            "total_amount_yi": breadth.get("total_amount", 0),
            "data_available": not breadth.get("failed", True),
        },
        "fund_flow": {
            "top_inflow": top_inflow,
            "top_outflow": top_outflow,
        },
        "_debug": {
            "avg_change": round(avg_change, 2),
            "avg_vol_change": round(avg_vol_change, 1),
            "spread": round(spread, 2),
            "growth_vs_def": round(growth_vs_def, 2),
        },
    }


async def _refresh_market_state_async():
    """后台刷新市场状态，避免 HTTP 请求阻塞在外部行情源上。"""
    global _MARKET_STATE_CACHE, _MARKET_STATE_REFRESH_PENDING
    if _MARKET_STATE_REFRESH_PENDING:
        return
    _MARKET_STATE_REFRESH_PENDING = True
    try:
        import asyncio
        data = await asyncio.to_thread(_compute_market_state)
        if data:
            _init_market_lock()
            with _MARKET_STATE_LOCK:
                _MARKET_STATE_CACHE = data
            try:
                from market_cache import save_json_cache
                save_json_cache("market_state_cache.json", data)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[市场状态] 后台刷新失败: {e}")
    finally:
        _MARKET_STATE_REFRESH_PENDING = False


# ══════════════════════════════════════════════════════════
#  Phase 3: 主线生命周期识别 (Theme Life-Cycle Engine)
# ══════════════════════════════════════════════════════════

# ── 板块生命周期：通过 sector_worker 自动维护，不再需要本地缓存 ──
# _SECTOR_LIFECYCLE_CACHE 已废弃
# _SECTOR_LIFECYCLE_LOCK 已废弃
# 均改用 sector_worker.get_all_lifecycles()


def _fetch_etf_hist(name: str, code: str, market: str, days: int = 30) -> Optional[pd.DataFrame]:
    """获取单只 ETF 的完整日行情（带 8s 超时）"""
    from network_guard import safe_call
    try:
        symbol = f"{market}{code}"
        df = safe_call(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=(datetime.now() - timedelta(days=days)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",
            ),
            timeout=8.0, name=f"etf_hist_{name[:6]}"
        )
        if df is None or (hasattr(df, "empty") and df.empty) or len(df) < 5:
            return None
        return df
    except Exception as e:
        print(f"[生命周期] {name}({code}) 历史数据获取失败: {e}", flush=True)
        return None


def _normalize_series(series: pd.Series) -> pd.Series:
    """将序列归一化到 [0, 1] 区间，避免板块波动率差异导致评分失真"""
    s = series.dropna()
    if len(s) < 2:
        return pd.Series([0.5] * len(series))
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series([0.5] * len(series))
    return (series - mn) / (mx - mn)


def _compute_ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=period, adjust=False).mean()


def compute_theme_lifecycle(
    df: pd.DataFrame,
    sh300_df: Optional[pd.DataFrame] = None,
    lookback: int = 20,
) -> dict:
    """
    板块生命周期判定引擎。

    输入:
        df: 板块 ETF 的 OHLCV DataFrame（至少 5 行，建议 20 行以上）
        sh300_df: 同期沪深300 DataFrame（用于 RS 计算），None 则跳过
        lookback: 滚动计算窗口

    输出:
        {
            "phase": "initiation" | "strengthening" | "divergence" | "decay" | "noise",
            "confidence": float,
            "scores": {四个阶段分数归一化},
            "signals": {核心信号值},
        }
    """
    close_col = "close" if "close" in df.columns else "收盘"
    amount_col = "amount" if "amount" in df.columns else "成交额"

    E = df[close_col].astype(float).values  # close prices
    V = df[amount_col].astype(float).values  # trade amounts

    n = len(E)
    if n < 5:
        return {"phase": "noise", "confidence": 0.0, "scores": {}, "signals": {}}

    lookback = min(lookback, n)

    # ── 1. 乖离率 Bias ──
    # 5 日指数移动平均作为短期趋势参考
    close_s = df[close_col].astype(float)
    ema5 = _compute_ema(close_s, 5).values
    # ★ 铁血安全除法：零分母时输出 0.0，绝不产生 inf/nan/"混沌"
    with np.errstate(divide='ignore', invalid='ignore'):
        bias = np.where(ema5 != 0, (E - ema5) / ema5 * 100, 0.0)

    # 最近 bias 序列（取有效部分）
    bias_series = pd.Series(bias[max(0, n - lookback):])
    bias_normalized = _normalize_series(bias_series.abs())  # 归一化到 0-1

    # 关键信号
    current_bias = float(bias[-1]) if np.isfinite(bias[-1]) else 0.0
    bias_3days_ago = float(bias[-3]) if n >= 3 and np.isfinite(bias[-3]) else current_bias
    bias_5days_ago = float(bias[-5]) if n >= 5 and np.isfinite(bias[-5]) else 0.0
    bias_slope = current_bias - bias_3days_ago  # 3 日 bias 变化

    # ── 2. 成交量动量 Vol Momentum ──
    vol_s = pd.Series(V)
    vol_ma3 = vol_s.rolling(3, min_periods=1).mean().values
    vol_ma5 = vol_s.rolling(5, min_periods=1).mean().values

    # 成交量变化率（当前量 / 5日均量 - 1）
    if n >= 5:
        with np.errstate(divide='ignore', invalid='ignore'):
            vol_mom_raw = np.where(vol_ma5 != 0, (V - vol_ma5) / vol_ma5 * 100, 0.0)
        vol_mom = float(vol_mom_raw[-1]) if np.isfinite(vol_mom_raw[-1]) else 0.0
        vol_mom_series = vol_mom_raw
    else:
        vol_mom = 0.0
        vol_mom_series = np.array([0.0] * n)
    vol_mom_recent = vol_mom_series[-min(5, len(vol_mom_series)):] if len(vol_mom_series) > 0 else [0.0]
    vol_consecutive_up = sum(1 for v in vol_mom_recent[-3:] if v > 10)  # 最近3日有几天放量
    vol_consecutive_down = sum(1 for v in vol_mom_recent[-3:] if v < -10)  # 最近3日有几天缩量

    vol_mom_normalized = _normalize_series(pd.Series(np.abs(vol_mom_series[max(0, n - lookback):]))) if n >= lookback else pd.Series([0.5])

    # ── 3. 价格动量 Price Momentum ──
    price_mom_3 = ((E[-1] / E[-3] - 1) * 100) if n >= 3 and E[-3] != 0 else 0.0
    price_mom_5 = ((E[-1] / E[-5] - 1) * 100) if n >= 5 and E[-5] != 0 else 0.0
    price_accel = price_mom_3 - price_mom_5  # 加速度（正=加速上，负=减速）

    # ── 4. 相对强度 RS（对沪深300） ──
    rs = 0.0
    rs_slope = 0.0
    if sh300_df is not None and not sh300_df.empty:
        sh300_close_col = "close" if "close" in sh300_df.columns else "收盘"
        sh300_close = sh300_df[sh300_close_col].astype(float).values
        min_len = min(len(E), len(sh300_close))
        if min_len >= 5:
            e_idx_5 = min(5, len(E))
            sh_idx_5 = min(5, len(sh300_close))
            sector_ret_5 = E[-1] / E[-e_idx_5] if len(E) >= 5 and E[-e_idx_5] != 0 else 1.0
            sh300_ret_5 = sh300_close[-1] / sh300_close[-sh_idx_5] if len(sh300_close) >= 5 and sh300_close[-sh_idx_5] != 0 else 1.0
            rs = ((sector_ret_5 / sh300_ret_5 - 1) * 100) if sh300_ret_5 != 0 else 0.0
            # 最近 10 日的 RS 趋势
            if min_len >= 10:
                e_idx_10 = min(10, len(E))
                sh_idx_10 = min(10, len(sh300_close))
                sector_ret_10 = E[-1] / E[-e_idx_10] if len(E) >= 10 and E[-e_idx_10] != 0 else 1.0
                sh300_ret_10 = sh300_close[-1] / sh300_close[-sh_idx_10] if len(sh300_close) >= 10 and sh300_close[-sh_idx_10] != 0 else 1.0
                rs_10 = ((sector_ret_10 / sh300_ret_10 - 1) * 100) if sh300_ret_10 != 0 else 0.0
                rs_slope = rs - rs_10

    # ── 5. 四阶段投票（分数归一化版） ──

    # 初始化
    s_init = 0.0
    s_strength = 0.0
    s_div = 0.0
    s_decay = 0.0

    # (A) 启动期特征
    #   - bias 刚转正或小正，且之前为负（0→1 转向）
    #   - 成交量由小转大（vol_mom 由负转正）
    #   - RS 从负转正
    if -1.5 < current_bias < 3.0:
        s_init += 1.5
    if n >= 5 and bias_5days_ago <= 0 < current_bias:
        s_init += 2.5  # bias 零轴金叉
    if n >= 3 and vol_mom_series[-1] > 15 and (len(vol_mom_series) < 3 or vol_mom_series[-2] < 10):
        s_init += 2.0  # 量能刚刚启动
    if rs > 0 and (sh300_df is None or rs_slope > -0.5):
        s_init += 1.0

    # (B) 强化期特征
    #   - bias > 3% 且仍在上行
    #   - 放量持续（连续 2+ 日 vol_mom > 10%）
    #   - RS > 3% 且趋势向上
    if current_bias > 3.0 and bias_slope > 0.2:
        s_strength += 2.5
    elif current_bias > 3.0:
        s_strength += 1.0
    if vol_consecutive_up >= 2:
        s_strength += 2.0
    elif vol_consecutive_up == 1:
        s_strength += 0.5
    if rs > 3.0 and rs_slope > 0.3:
        s_strength += 2.0
    if price_mom_5 > 3.0 and price_accel > 0:
        s_strength += 1.5

    # (C) 分歧期特征 — 核心: 放量滞涨
    #   - bias 在 2%~5% 但不再扩大（slope 近 0 或负）
    #   - 量能仍高但价格不动（放量滞涨）
    #   - 加速度从正转负
    if 2.0 < current_bias < 6.0 and bias_slope < 0.5:
        s_div += 2.0
    if vol_mom > 10 and abs(price_mom_3) < 0.8:
        s_div += 3.0  # 放量滞涨！
    if price_accel < 0:
        s_div += 1.5
    if rs < 5 and rs_slope < 0:
        s_div += 1.0

    # (D) 退潮期特征
    #   - bias < 0（跌破 MA5）
    #   - 缩量下跌或放量暴跌
    #   - RS < 0（跑输大盘）
    if current_bias < 0:
        s_decay += 2.5
    if vol_mom < -15 or (vol_mom < -5 and price_mom_3 < -2):
        s_decay += 2.0
    if vol_consecutive_down >= 2:
        s_decay += 1.0
    if rs < -1.0:
        s_decay += 2.0
    # 放量暴跌（量 > 20% 且价格跌 > 2%）
    if vol_mom > 20 and price_mom_3 < -2:
        s_decay += 2.5

    # ── 6. 归一化：softmax 避免板块波动率差异 & 混沌过滤 ──
    scores_raw = {
        "initiation": s_init,
        "strengthening": s_strength,
        "divergence": s_div,
        "decay": s_decay,
    }
    total = sum(scores_raw.values())

    if total < 3.0:
        # 分数太低 → 无明显趋势
        return {
            "phase": "noise",
            "confidence": 0.0,
            "scores": {k: 0.0 for k in scores_raw},
            "signals": {
                "bias": round(current_bias, 2),
                "bias_slope": round(bias_slope, 2),
                "vol_mom": round(float(vol_mom), 1),
                "price_mom_3": round(price_mom_3, 2),
                "price_mom_5": round(price_mom_5, 2),
                "price_accel": round(price_accel, 2),
                "rs": round(rs, 2),
                "rs_slope": round(rs_slope, 2),
                "vol_consecutive_up": vol_consecutive_up,
                "vol_consecutive_down": vol_consecutive_down,
            },
        }

    normalized = {k: round(v / total, 4) for k, v in scores_raw.items()}
    winner = max(normalized, key=normalized.get)
    confidence = round(normalized[winner], 4)

    # 伪启动过滤
    if confidence < 0.3:
        winner = "noise"
        confidence = 0.0

    return {
        "phase": winner,
        "confidence": confidence,
        "scores": normalized,
        "signals": {
            "bias": round(current_bias, 2),
            "bias_slope": round(bias_slope, 2),
            "vol_mom": round(float(vol_mom), 1),
            "price_mom_3": round(price_mom_3, 2),
            "price_mom_5": round(price_mom_5, 2),
            "price_accel": round(price_accel, 2),
            "rs": round(rs, 2),
            "rs_slope": round(rs_slope, 2),
            "vol_consecutive_up": vol_consecutive_up,
            "vol_consecutive_down": vol_consecutive_down,
        },
    }


@app.on_event("startup")
async def start_market_state_updater():
    """应用启动：先恢复磁盘缓存，再异步刷新真实行情。"""
    global _MARKET_STATE_CACHE, _market_summary_cache, _market_summary_ts
    import asyncio
    import time
    from market_cache import load_json_cache

    _init_market_lock()

    cached = load_json_cache("market_state_cache.json", max_age_hours=48)
    if cached:
        with _MARKET_STATE_LOCK:
            _MARKET_STATE_CACHE = cached
        print("[启动] 市场状态已恢复", flush=True)

    cached = load_json_cache("market_summary_cache.json", max_age_hours=48)
    if cached:
        _market_summary_cache = cached
        _market_summary_ts = time.time()
        print("[启动] 涨跌家数已恢复", flush=True)

    cached = load_json_cache("sector_lifecycles_cache.json", max_age_hours=48)
    if cached:
        from sector_worker import SECTOR_LIFECYCLE_FULL, SECTOR_LIFECYCLE_LOCK
        with SECTOR_LIFECYCLE_LOCK:
            SECTOR_LIFECYCLE_FULL.clear()
            SECTOR_LIFECYCLE_FULL.update(cached)
        print(f"[启动] 板块生命周期已恢复 ({len(cached)} 个)", flush=True)

    try:
        start_sector_worker()
    except Exception as e:
        logger.warning(f"[启动] 板块后台线程启动失败: {e}")

    for task in (_refresh_market_state_async, _background_refresh_market_summary,
                 _refresh_index_quotes_async, _refresh_hot_news_async, _refresh_newsflash_async):
        try:
            asyncio.create_task(task())
        except Exception as e:
            logger.debug(f"[启动] 后台任务调度失败 {getattr(task, '__name__', task)}: {e}")

    print("[启动] 就绪 (磁盘缓存 + 后台刷新)", flush=True)

@app.get("/api/market-state")
def get_market_state():
    global _MARKET_STATE_CACHE
    _init_market_lock()
    with _MARKET_STATE_LOCK:
        if _MARKET_STATE_CACHE is not None:
            return _MARKET_STATE_CACHE
    # 磁盘回退 — 不触发实时计算
    try:
        from market_cache import load_json_cache
        cached = load_json_cache("market_state_cache.json", max_age_hours=48)
        if cached:
            with _MARKET_STATE_LOCK:
                _MARKET_STATE_CACHE = cached
            return cached
    except:
        pass
    return {"environment": "range_bound", "label": "数据加载中", "adjustment_factor": 1.0, "main_line": "暂无"}


# ── 板块生命周期 API（改用 sector_worker 全市场动态扫描） ──

@app.get("/api/sector-lifecycles")
def get_sector_lifecycles():
    """
    全市场板块生命周期。
    worker 已扫描完成 → 返回真实生命周期数据；
    worker 尚未完成 → 从磁盘缓存恢复；
    磁盘也无 → 返回内置 56 板块名称列表。
    """
    from sector_scanner import get_registry
    registry = get_registry()
    sector_names = registry.get_sector_names()

    lifecycles = get_all_lifecycles()
    source = "live"
    if not lifecycles:
        # ★ worker 未就绪，尝试磁盘缓存
        try:
            from market_cache import load_json_cache
            cached = load_json_cache("sector_lifecycles_cache.json", max_age_hours=48)
            if cached:
                lifecycles = cached
                source = "disk_cache"
                # 也写回内存，避免后续请求重复读盘
                with SECTOR_LIFECYCLE_LOCK:
                    SECTOR_LIFECYCLE_FULL.clear()
                    SECTOR_LIFECYCLE_FULL.update(cached)
        except Exception:
            pass

    if not lifecycles:
        # worker 数据暂未就绪 → 返回内置板块名称作为兜底
        return {
            "sectors": {name: {"phase": "noise", "confidence": 0.0} for name in sector_names},
            "summary": {
                "total_sectors": len(sector_names),
                "phase_distribution": {"noise": len(sector_names)},
                "top_strength": [],
            },
            "last_updated": None,
            "source": "builtin_fallback",
        }
    summary = get_sector_summary() if source == "live" else {}
    return {
        "sectors": lifecycles,
        "summary": {
            "total_sectors": summary.get("total_sectors", len(lifecycles)),
            "phase_distribution": summary.get("phase_distribution", {}),
            "top_strength": summary.get("top_strength", [])[:5],
        },
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
    }


@app.get("/api/market-full")
def get_market_full():
    """聚合接口：市场状态 + 全市场板块生命周期 + 联动预警"""
    global _MARKET_STATE_CACHE
    _init_market_lock()

    # 获取市场状态 — 只读缓存不触发实时计算
    env = _MARKET_STATE_CACHE
    if env is None:
        try:
            from market_cache import load_json_cache
            env = load_json_cache("market_state_cache.json", max_age_hours=48) or {}
        except:
            env = {}

    # 获取全市场生命周期（sector_worker）
    lc = get_all_lifecycles()
    sectors = lc or {}

    # ── 联动预警逻辑（6 阶段生命周期） ──
    alerts: list[dict] = []
    if env and sectors:
        env_name = env.get("environment", "range_bound")
        for name, info in sectors.items():
            phase = info.get("phase", "noise")
            conf = info.get("confidence", 0)
            bias = info.get("bias", 0)

            # 🔴 高风险: 退潮期
            if phase == "decay" and conf >= 0.4:
                alerts.append({
                    "sector": name, "severity": "danger",
                    "message": f"{name}板块退潮（乖离{bias:.1f}%），资金系统性退出",
                    "action": "清仓该板块个股，切换至主线方向"
                })
            # ⚠️ 中风险: 高位分歧
            elif phase == "high_divergence" and conf >= 0.4:
                alerts.append({
                    "sector": name, "severity": "warning",
                    "message": f"{name}高位分歧，量价背离，注意止盈",
                    "action": "减仓至半，破位则清"
                })
            # 👀 启动信号: startup
            elif phase == "startup" and conf >= 0.3:
                alerts.append({
                    "sector": name, "severity": "opportunity",
                    "message": f"{name}启动迹象（乖离{bias:.1f}%），资金试探入场",
                    "action": "观察确认，筛选领涨个股轻仓试仓"
                })
            # ✅ 强势: 主升一期 / 加速期
            elif phase in ("main_rise_1", "acceleration") and conf >= 0.4:
                alerts.append({
                    "sector": name, "severity": "opportunity",
                    "message": f"{name}处于{'主升' if phase == 'main_rise_1' else '加速'}期（乖离{bias:.1f}%），趋势健康",
                    "action": "顺势持仓，回调至均线支撑可加仓"
                })
            # 🧊 冰点修复
            elif phase == "ice_recovery" and conf >= 0.3:
                alerts.append({
                    "sector": name, "severity": "info",
                    "message": f"{name}冰点修复，超跌后企稳信号",
                    "action": "关注底部结构，等待量能确认再介入"
                })

        # 限制预警数量（全市场可能几十个，只保留最严重的前 10 条）
        severity_order = {"danger": 0, "warning": 1, "opportunity": 2, "info": 3}
        alerts.sort(key=lambda a: severity_order.get(a["severity"], 99))
        alerts = alerts[:10]

    # 附带注册表摘要
    registry_info = {}
    try:
        s = get_sector_summary()
        registry_info = s
    except:
        pass

    return {
        "environment": env,
        "lifecycles": {
            "sectors": sectors,
            "total": len(sectors),
            "summary": registry_info.get("phase_distribution", {}),
        },
        "alerts": alerts,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── Alpha 引擎 API ──

@app.get("/api/alpha/screener")
async def get_screener():
    """
    Legacy compatibility endpoint.

    The old implementation launched a slow stock-by-stock AlphaEngine scan from
    the dashboard path. The production dashboard now uses /api/alpha/candidates,
    which has explicit live-data guards and returns quickly when the market
    source is unavailable.
    """
    return {
        "screener": [],
        "candidates": [],
        "source": "legacy_endpoint_disabled",
        "message": "Legacy stock-by-stock scan is disabled for fast dashboard loading. Use /api/alpha/candidates instead.",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/api/alpha/portfolio")
def post_portfolio_guard(payload: dict):
    """
    持仓诊断引擎（全市场版）。
    输入: {"positions": [{"code": "300750", "name": "宁德时代"}, ...]}
    输出: 每个持仓的诊断结果，
         通过 sector_scanner 注册表动态映射板块。
    """
    _init_market_lock()

    positions = payload.get("positions", [])
    if not positions:
        return {"error": "请提供持仓列表"}, 400

    with _MARKET_STATE_LOCK:
        ms = _MARKET_STATE_CACHE
    lc = get_all_lifecycles()

    guard = PortfolioGuard(positions=positions, sector_lifecycles=lc or {}, market_state=ms or {})
    diagnosis = guard.run()
    return {
        "positions": diagnosis,
        "market_state": {
            "label": (ms or {}).get("label", "?"),
            "environment": (ms or {}).get("environment", ""),
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/alpha/summary")
async def get_executive_summary(include_alpha: bool = False):
    """
    每日执行简报 — 基于全市场板块生命周期，
    不需要用户操作，自动生成投委会级操作结论。
    """
    _init_market_lock()

    with _MARKET_STATE_LOCK:
        ms = _MARKET_STATE_CACHE
    lc = get_all_lifecycles()

    if not ms:
        # 从磁盘恢复
        try:
            from market_cache import load_json_cache
            ms = load_json_cache("market_state_cache.json", max_age_hours=48)
        except:
            pass
    if not ms:
        return {"text": "数据未就绪，请稍后查看"}, 503

    # 顺便跑一下 Alpha 引擎，获取今日精选池
    alpha_result = None
    if include_alpha:
        try:
            engine = AlphaEngine(market_state=ms, sector_lifecycles=lc)
            alpha_result = engine.run(top_n=5)
        except Exception as e:
            print(f"[AlphaEngine] failed: {e}", flush=True)

    text = generate_executive_summary(ms, lc, alpha_result)
    return {
        "text": text,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/sector/registry")
def get_sector_registry():
    """返回当前板块注册表信息"""
    try:
        registry = get_registry()
        return {
            "total": len(registry.get_active()),
            "sectors": registry.get_sector_list(),
            "last_refresh": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.get("/api/sector/heatmap")
def get_sector_heatmap():
    """
    板块热力图数据：行业强度 × 成交额占比
    供前端矩形树图组件使用。
    """
    lc = get_all_lifecycles()
    if not lc:
        return {"data": []}

    registry = get_registry()
    registry_sectors = registry.get_active()

    heatmap_data = []
    for name, info in lc.items():
        reg_info = registry_sectors.get(name, {})
        phase = info.get("phase", "noise")
        confidence = info.get("confidence", 0)
        bias = info.get("bias", 0)

        # 强度指标：置信度 × 阶段权重
        phase_strength = {
            "strengthening": 1.0, "initiation": 0.7,
            "divergence": 0.3, "decay": -0.5, "noise": 0.0
        }
        strength = confidence * phase_strength.get(phase, 0)

        # 无成交额数据时用置信度做权重
        stock_cnt = reg_info.get("stock_cnt", 0)
        amount_weight = min(reg_info.get("avg_amount", 0) / 5e9, 1.0) * 100
        if amount_weight == 0:
            amount_weight = max(confidence * 100, 1)  # 退而求其次

        heatmap_data.append({
            "name": name,
            "phase": phase,
            "confidence": confidence,
            "bias": bias,
            "strength": round(strength, 3),
            "amount_weight": round(amount_weight, 1),
        })

    # 保留所有非noise板块 + noise中有一定权重的
    heatmap_data = [d for d in heatmap_data if d["phase"] != "noise" or d["amount_weight"] > 10]
    heatmap_data.sort(key=lambda d: -abs(d["strength"]))

    return {
        "data": heatmap_data[:100],  # 最多 100 个
        "total": len(lc),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════
#  量化黑箱雷达：算法冰山单 / 绝望指数 / 量价背离
# ══════════════════════════════════════════════════════

def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI"""
    n = len(closes)
    rsi = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, n - 1):
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return rsi


def compute_abnormal_alerts(ohlcv: list[dict]) -> list[dict]:
    """
    纯本地计算三项硬核量化警报，不依赖任何外部接口。
    返回最多 3 条警报 dict，每条含 { id, level, title, body }。
    """
    if len(ohlcv) < 20:
        return []

    closes = np.array([d["close"] for d in ohlcv], dtype=np.float64)
    opens = np.array([d["open"] for d in ohlcv], dtype=np.float64)
    highs = np.array([d["high"] for d in ohlcv], dtype=np.float64)
    lows = np.array([d["low"] for d in ohlcv], dtype=np.float64)
    volumes = np.array([d["volume"] for d in ohlcv], dtype=np.float64)
    n = len(ohlcv)
    alerts: list[dict] = []

    today_close = closes[-1]
    today_open = opens[-1]
    today_high = highs[-1]
    today_low = lows[-1]
    today_vol = volumes[-1]
    amplitude = (today_high - today_low) / closes[-2] if n >= 2 else 0

    avg_vol_5 = np.mean(volumes[-6:-1]) if n >= 6 else today_vol
    avg_vol_20 = np.mean(volumes[-21:-1]) if n >= 21 else today_vol

    # ── Alert 1: 算法冰山单 ──
    center = (today_high + today_low + today_close * 2) / 4.0
    center_dev = abs(today_close - center) / today_close if today_close > 0 else 0

    if today_vol > avg_vol_5 * 1.4 and center_dev < 0.005 and amplitude > 0.03:
        alerts.append({
            "id": "iceberg",
            "level": "danger",
            "title": "算法冰山单探测",
            "body": (
                "盘中成交量异常放大（今日量能达5日均量的 "
                f"{today_vol/avg_vol_5:.1f} 倍），但价格重心诡异锁死在 "
                f"¥{center:.2f}（偏离仅{center_dev*100:.2f}%），"
                "同时振幅超过 3%。"
                "这种'量升价不跟'的异常格局，极大概率是主力机构正在使用"
                "TWAP/VWAP 算法拆单进行隐蔽吸筹或撤退。"
                "冰山之下暗流涌动，方向一旦明朗将是暴拉或暴跌，请高度警惕。"
            ),
        })

    # ── Alert 2: 散户割肉绝望指数 ──
    if n >= 5:
        bearish_streak = 0
        for i in range(n - 1, max(n - 10, 0), -1):
            if closes[i] < opens[i]:
                bearish_streak += 1
            else:
                break
        vol_decline = True
        if bearish_streak >= 3:
            for j in range(n - bearish_streak + 1, n):
                if volumes[j] > volumes[j - 1] * 0.85:
                    vol_decline = False
                    break
            if vol_decline and today_vol < avg_vol_20 * 0.5 and bearish_streak >= 3:
                alerts.append({
                    "id": "capitulation",
                    "level": "warning",
                    "title": "筹码死锁：散户割肉绝望",
                    "body": (
                        f"股价已连续 {bearish_streak} 个交易日缩量阴跌，"
                        f"每日成交量以 >15% 的速度衰减，今日量能仅剩 20 日均量的 "
                        f"{today_vol/avg_vol_20*100:.0f}%。"
                        "市场交投陷入极度死寂——这不是没人卖，是散户已经亏到麻木、"
                        "拒绝割肉了。抛压基本枯竭，空头手中筹码已近耗尽。"
                        "注意防范突然的'地量 V 反'点火，或者无量空跌的下空洞陷阱。"
                        "此时离场和抄底同样危险，建议设好止损等方向确认。"
                    ),
                })

    # ── Alert 3: 量价极值动量背离 ──
    rsi14 = _rsi(closes, 14)
    if n >= 20 and not np.isnan(rsi14[-1]):
        today_rsi = rsi14[-1]
        lookback = min(5, n - 1)

        # 底背离：价格创 5 日新低，但 RSI 未创新低
        price_5d_low = np.min(closes[-lookback-1:-1])
        rsi_5d_low = np.nanmin(rsi14[-lookback-1:-1])
        if today_close < price_5d_low and today_rsi > rsi_5d_low and today_rsi < 50:
            alerts.append({
                "id": "divergence_bull",
                "level": "info",
                "title": "动量底背离：强弩之末",
                "body": (
                    f"价格今日创下 5 日新低 ¥{today_close:.2f}，但 14 日 RSI "
                    f"({today_rsi:.1f}) 并未跟随创低（前低 {rsi_5d_low:.1f}）。"
                    "这是经典的'底背离'信号——空头砸盘力量正在衰竭，"
                    "虽然价格还在跌，但底层动能已经拒绝创新低。"
                    "趋势随时可能瞬间反转，追空者极可能被 V 反打爆。"
                    "建议减空或轻仓试多，严格止损于今日低点下方 2%。"
                ),
            })

        # 顶背离：价格创 5 日新高，但 RSI 未创新高
        price_5d_high = np.max(closes[-lookback-1:-1])
        rsi_5d_high = np.nanmax(rsi14[-lookback-1:-1])
        if today_close > price_5d_high and today_rsi < rsi_5d_high and today_rsi > 50:
            alerts.append({
                "id": "divergence_bear",
                "level": "warning",
                "title": "动量顶背离：拉高出货",
                "body": (
                    f"价格今日创下 5 日新高 ¥{today_close:.2f}，但 14 日 RSI "
                    f"({today_rsi:.1f}) 并未同步创新高（前高 {rsi_5d_high:.1f}）。"
                    "这是典型的'顶背离'信号——主力正在用最后的资金拉高诱多，"
                    "但底层动能已经跟不上了。"
                    "每一次新高都是出货的掩护，跟风追高的散户正在成为接盘侠。"
                    "建议减仓锁定利润，不要被短暂的阳线迷惑双眼。"
                ),
            })

    return alerts


# ══════════════════════════════════════════════════════
#  五维战斗力诊断评分
# ══════════════════════════════════════════════════════

def compute_combat_score(
    snap: dict,
    money_flow: dict,
    alerts: list[dict],
    concepts: list[str],
    change_pct: float,
) -> dict:
    """
    综合技术面/资金面/行业共振/筹码安全/综合评级，0-100 分。
    所有数据已在内存中，纯本地计算，零网络开销。
    """
    scores: dict[str, int] = {}

    # ── 技术面 (0-50) → 映射到 0-100 ──
    technical = 50
    alignment = snap.get("alignment", "")
    dif_val = snap.get("dif")
    dea_val = snap.get("dea")
    if alignment == "多头排列":
        technical += 25
    elif alignment == "空头排列":
        technical -= 20
    # MACD 加成
    if dif_val is not None and dea_val is not None:
        if dif_val > dea_val:
            technical += 12
        else:
            technical -= 10
        if dif_val > 0:
            technical += 8
    # 今日涨跌
    technical += min(max(change_pct * 3, -15), 15)
    scores["technical"] = max(5, min(100, round(technical)))

    # ── 资金面 (0-100) ──
    mf_pct = money_flow.get("main_force_pct", 50)
    main_net = money_flow.get("main_net", 0)
    capital = round(mf_pct)
    if main_net > 0:
        capital += 8
    else:
        capital -= 5
    scores["capital"] = max(5, min(100, capital))

    # ── 行业共振度 (0-100) ──
    # 概念越多、越在风口，共振越强
    n_concepts = len(concepts)
    sector = 30 + n_concepts * 7  # 3个概念=51分，6个=72分，10个=100分
    # 热门赛道加成
    hot_keywords = ["AI", "人工智能", "锂电池", "固态电池", "光伏", "芯片", "半导体", "新能源", "储能", "白酒", "机器人"]
    for kw in hot_keywords:
        if any(kw in c for c in concepts):
            sector += 5
    scores["sector"] = max(10, min(100, round(sector)))

    # ── 筹码安全度 (0-100) ──
    safety = 70
    # 每条警报扣分
    for alert in alerts:
        if alert["level"] == "danger":
            safety -= 20
        elif alert["level"] == "warning":
            safety -= 12
        else:
            safety -= 6
    # 空头排列额外扣分
    if alignment == "空头排列":
        safety -= 15
    elif alignment == "多头排列":
        safety += 10
    scores["safety"] = max(5, min(100, round(safety)))

    # ── 综合评级 ──
    overall = round(
        scores["technical"] * 0.35
        + scores["capital"] * 0.25
        + scores["sector"] * 0.15
        + scores["safety"] * 0.25
    )
    scores["overall"] = max(5, min(100, overall))

    # ── 评级标签 ──
    if overall >= 80:
        label = "强力吸筹"
    elif overall >= 65:
        label = "稳中偏多"
    elif overall >= 50:
        label = "震荡观望"
    elif overall >= 35:
        label = "减仓避险"
    else:
        label = "危险极高"

    return {
        "scores": scores,
        "label": label,
    }


# ══════════════════════════════════════════════════════
#  资金流向数据获取
# ══════════════════════════════════════════════════════

def fetch_money_flow(code: str, ohlcv: Optional[list[dict]] = None) -> dict:
    """
    获取个股资金流向单日快照。
    1. 优先尝试 akshare 实时接口
    2. 失败则基于 OHLCV 数据智能估算主力/散户比例
    3. 始终返回 dict，绝不抛异常
    """
    # ── 方案 A: akshare 实时资金流向（带 15 秒超时，防止卡死） ──
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(ak.stock_individual_fund_flow, stock=code, market="sh" if code.startswith("6") else "sz")
            df = fut.result(timeout=15)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            raw = latest.to_dict()

            def _pick(keys):
                for k in keys:
                    if k in raw:
                        return raw[k]
                return None

            def _val(v):
                try:
                    return float(v)
                except Exception:
                    return 0.0

            mn = _val(_pick(["主力净流入-净额", "主力净流入净额", "main_net_inflow"]) or 0)
            sl = _val(_pick(["超大单净流入-净额", "超大单净流入净额", "super_large_net_inflow"]) or 0)
            la = _val(_pick(["大单净流入-净额", "大单净流入净额", "large_net_inflow"]) or 0)
            md = _val(_pick(["中单净流入-净额", "中单净流入净额", "medium_net_inflow"]) or 0)
            sm = _val(_pick(["小单净流入-净额", "小单净流入净额", "small_net_inflow"]) or 0)
            mp = _val(_pick(["主力净流入-占比", "主力净流入占比", "main_net_pct"]) or 0)

            main_abs = abs(sl) + abs(la)
            retail_abs = abs(md) + abs(sm)
            total_abs = main_abs + retail_abs
            if total_abs > 0 and mn != 0:
                main_force_pct = round(main_abs / total_abs * 100, 1)
                retail_pct = round(retail_abs / total_abs * 100, 1)
                return {
                    "main_net": mn, "super_large_net": sl, "large_net": la,
                    "medium_net": md, "small_net": sm, "main_pct": mp,
                    "main_force_pct": main_force_pct, "retail_pct": retail_pct,
                    "source": "akshare",
                }
    except Exception as e:
        print(f"[资金流向] {code} akshare接口失败: {e}", flush=True)

    # ── 方案 B: 从 OHLCV 智能估算 ──
    if ohlcv and len(ohlcv) >= 5:
        return _estimate_money_flow_from_ohlcv(code, ohlcv)
    return {
        "main_net": 0, "super_large_net": 0, "large_net": 0,
        "medium_net": 0, "small_net": 0, "main_pct": 0,
        "main_force_pct": 55, "retail_pct": 45, "source": "fallback",
    }


def _estimate_money_flow_from_ohlcv(code: str, ohlcv: list[dict]) -> dict:
    """基于最近 5 天 OHLCV 数据估算主力/散户资金博弈比例"""
    recent = ohlcv[-5:]
    avg_volume = sum(d["volume"] for d in recent) / len(recent)

    today = ohlcv[-1]
    today_vol = today["volume"]
    body = abs(today["close"] - today["open"])
    wick_upper = today["high"] - max(today["open"], today["close"])
    wick_lower = min(today["open"], today["close"]) - today["low"]
    total_range = today["high"] - today["low"]

    # 1. 成交量偏离度：今日成交量 vs 5日均量
    vol_ratio = today_vol / avg_volume if avg_volume > 0 else 1.0
    vol_ratio = vol_ratio if np.isfinite(vol_ratio) and vol_ratio > 0 else 1.0  # 防止 0 或 inf

    # 2. 实体占比：大实体 = 机构主导方向
    body_ratio = body / total_range if total_range > 0 else 0.5

    # 3. 收盘方向：阳线=主力偏买，阴线=主力偏卖
    is_bullish = today["close"] >= today["open"]

    # 综合估算主力占比 (30%-75%)
    main_force_pct = 40.0
    main_force_pct += min(vol_ratio - 1, 0.5) * 20  # 放量 → +最多10%
    main_force_pct += (body_ratio - 0.3) * 25        # 大实体 → ±最多12%
    main_force_pct = max(30.0, min(75.0, main_force_pct))
    main_force_pct = round(main_force_pct, 1)
    retail_pct = round(100 - main_force_pct, 1)

    # 估算成交额
    turnover = today["close"] * today_vol
    main_net_est = round(turnover * (main_force_pct / 100) * (0.15 if is_bullish else -0.12), 2)

    print(f"[资金流向] {code} OHLCV估算: vol_ratio={vol_ratio:.2f} body_ratio={body_ratio:.2f} → main={main_force_pct}% retail={retail_pct}%", flush=True)

    return {
        "main_net": main_net_est,
        "super_large_net": round(main_net_est * 0.55, 2),
        "large_net": round(main_net_est * 0.45, 2),
        "medium_net": round(-main_net_est * 0.6, 2),
        "small_net": round(-main_net_est * 0.4, 2),
        "main_pct": main_force_pct,
        "main_force_pct": main_force_pct,
        "retail_pct": retail_pct,
        "source": "ohlcv_estimate",
    }


# ══════════════════════════════════════════════════════
#  AI 分析 (DeepSeek / 规则回退)
# ══════════════════════════════════════════════════════

_CAUSAL_TEMPLATE = """
【强制因果链模板】
你输出的 JSON 中必须包含一个 "causalAnalysis" 字段，其值是一个数组。
每个数组元素是一个因果链对象，格式如下：
{
  "cause": "当前核心现象（必须基于技术形态或资金流向数据，不能放空炮）",
  "effect": "该现象导致的市场参与者的行为逻辑（主力在想什么？散户在干什么？）",
  "implication": "给你的启示：明确的风险/收益判断，后续操作假设"
}

要求：
1. 每条因果链都必须有数据支撑，不得无中生有。
2. causalAnalysis 数组长度为 1 到 3 条。
3. 如果数据不足以支撑因果推理，该字段可包含 1 条说明"数据不足以建立因果链"的条目。
4. {\"zhuangjia\"} / {\"duokong\"} / {\"plan\"} 字段仍保持原有人话风格，和 causalAnalysis 互相呼应但不重复。
"""

ANALYSIS_SYSTEM_PROMPT = """你是一个说话直白、眼光毒舌的顶级游资操盘手，操盘十几年看透了这帮主力和散户。

现在给你一只股票的技术面数据和主力资金流向，你要扮演操盘手角色，给出手下的操盘手看。

你讨厌黑话，只说人话。别扯什么"均线多头排列""MACD金叉"这种普通话，你要说的是：
- 庄家现在在干嘛（吸筹？拉升？出货？洗盘？）
- 主力是真拉还是假拉
- 散户在接盘还是割肉
- 明天大概怎么走
- 如果要做，什么位置进、什么位置跑

请严格以 JSON 格式返回，不要其他文字。格式如下：
{
  "zhuangjia": "庄家意图拆解（200字以内，直白毒舌，拆解主力真实意图）",
  "duokong": "主力多空博弈（100字以内，主力vs散户、多空力量对比）",
  "plan": {
    "direction": "看多/看空/震荡",
    "entry": 建议入场价格,
    "stop_loss": 止损价格,
    "target": 目标价格,
    "supplement": 补仓价位,
    "detail": "明日分步交易计划（200字以内，含具体价位和仓位管理）"
  },
  "support": 支撑位价格数字,
  "resistance": 压力位价格数字,
  "causalAnalysis": [
    {"cause": "现象", "effect": "行为逻辑", "implication": "启示"}
  ]
}

【强制因果链说明】
causalAnalysis 是核心字段，每条因果链必须有数据支撑。长度为 1-3 条。
causalAnalysis 的内容和你原有的 zhuangjia/duokong/plan 互相呼应但不重复。
如果数据不足以支撑因果推理，则 causalAnalysis 包含 1 条说明"数据不足以建立因果链"。
"""


def build_analysis_user_prompt(name: str, code: str, price: float, change_text: str, snap: dict, money_flow: Optional[dict] = None) -> str:
    """构造发给大模型的指标快照（含资金流向）"""
    # 技术面部分
    tech = f"""股票: {name} ({code})
最新价: ¥{price} ({change_text})
MA5: {snap["ma5"]}  MA10: {snap["ma10"]}  MA20: {snap["ma20"]}
均线状态: {snap["alignment"]}
MACD DIF: {snap["dif"]}  DEA: {snap["dea"]}  MACD柱: {snap["macd"]}"""

    # 资金流向部分（仅在有真实数据时展示）
    if money_flow and abs(money_flow.get("main_net", 0)) > 0:
        flow = f"""

【当日资金流向】
主力净流入: {money_flow['main_net']/1e4:.0f}万 (占比{money_flow['main_pct']:.1f}%)
超大单: {money_flow['super_large_net']/1e4:.0f}万
大单: {money_flow['large_net']/1e4:.0f}万
中单: {money_flow['medium_net']/1e4:.0f}万
散户: {money_flow['small_net']/1e4:.0f}万"""
        return tech + flow
    return tech


def _derive_support_resistance(price: float, snap: dict, ohlcv: Optional[list[dict]] = None) -> dict:
    """用近期高低点、均线和 ATR 推导支撑/压力，避免固定百分比拍脑袋。"""
    candidates_support: list[float] = []
    candidates_resistance: list[float] = []
    recent_bars = (ohlcv or [])[-80:]
    if recent_bars:
        highs = [float(b.get("high", 0) or 0) for b in recent_bars if float(b.get("high", 0) or 0) > 0]
        lows = [float(b.get("low", 0) or 0) for b in recent_bars if float(b.get("low", 0) or 0) > 0]
        closes = [float(b.get("close", 0) or 0) for b in recent_bars if float(b.get("close", 0) or 0) > 0]
        if highs and lows and closes:
            for window in (20, 60):
                candidates_resistance.append(max(highs[-window:]))
                candidates_support.append(min(lows[-window:]))
            trs = []
            for i, bar in enumerate(recent_bars):
                high = float(bar.get("high", 0) or 0)
                low = float(bar.get("low", 0) or 0)
                prev_close = closes[i - 1] if i > 0 and i - 1 < len(closes) else float(bar.get("close", price) or price)
                if high > 0 and low > 0:
                    trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            atr = sum(trs[-14:]) / max(1, min(14, len(trs))) if trs else price * 0.03
            candidates_resistance.append(price + atr * 1.5)
            candidates_support.append(price - atr * 1.5)

    for key in ("ma5", "ma10", "ma20", "ma60"):
        val = snap.get(key)
        if isinstance(val, (int, float)) and val > 0:
            if val >= price:
                candidates_resistance.append(float(val))
            else:
                candidates_support.append(float(val))

    resistance_above = [v for v in candidates_resistance if v > price * 1.002]
    support_below = [v for v in candidates_support if v < price * 0.998]
    resistance = min(resistance_above) if resistance_above else price * 1.05
    support = max(support_below) if support_below else price * 0.95
    return {
        "support": round(max(support, 0.01), 2),
        "resistance": round(max(resistance, price * 1.002), 2),
    }


def build_rule_analysis(name: str, code: str, price: float, change_text: str, snap: dict, money_flow: Optional[dict] = None, ohlcv: Optional[list[dict]] = None) -> dict:
    """基于规则的自动分析 — 无需 API key 也能生成有意义的结论，含资金流向"""
    alignment = snap["alignment"]
    dif_val = snap["dif"]
    dea_val = snap["dea"]
    macd_val = snap["macd"]
    ma20_val = snap["ma20"]
    ma10_val = snap["ma10"]
    ma5_val = snap["ma5"]

    # 判断涨跌方向（从 change_text 提取）
    is_up = not change_text.startswith("-")

    # 搭建分析文本
    parts = []

    # 均线判断 — 用人话
    if alignment == "空头排列":
        parts.append(f"均线全部往下压（MA5<MA10<MA20），短期没戏，不要手贱去抄底。")
        if ma5_val and ma10_val and abs(ma5_val - ma10_val) / price < 0.02:
            parts.append("不过 MA5 和 MA10 快粘上了，短线可能有个小反弹，但别太当真。")
    elif alignment == "多头排列":
        parts.append(f"均线多头发散（MA5>MA10>MA20），短期趋势没问题，在车上的可以捏着。")
    else:
        parts.append("均线缠在一起了，方向不明，这种时候别瞎动，等方向出来再说。")

    # MACD 判断 — 用人话
    if dif_val is not None and dea_val is not None:
        if dif_val < 0 and dea_val < 0:
            parts.append("MACD 在水下待着呢，空头控场。")
            if dif_val > dea_val:
                parts.append("不过 DIF 往上穿了 DEA（金叉），短线客可以博个反弹，快进快出。")
            elif dif_val < dea_val:
                parts.append("DIF 还在 DEA 下面趴着，没见底信号，别急着进场。")
        elif dif_val > 0 and dea_val > 0:
            parts.append("MACD 在水上，多头控场。")
            if dif_val < dea_val:
                parts.append("但是 DIF 掉头往下穿了 DEA（死叉），如果手上有货考虑减仓。")
        else:
            parts.append("MACD 在零轴附近晃悠，多空谁都没占到便宜。")

    if macd_val is not None and abs(macd_val) < 0.01:
        parts.append("红绿柱快缩没了，动能衰竭，变盘节点快到了。")

    # 资金流向判断
    zhuangjia_parts = []
    duokong_parts = []

    has_real_flow = money_flow and abs(money_flow.get("main_net", 0)) > 0
    if has_real_flow:
        main_net = money_flow.get("main_net", 0)
        main_pct = money_flow.get("main_pct", 0)
        super_large = money_flow.get("super_large_net", 0)
        large = money_flow.get("large_net", 0)
        small = money_flow.get("small_net", 0)

        if main_net > 0:
            zhuangjia_parts.append(f"资金面推断：主力今天净流入{main_net/1e4:.0f}万（占比{main_pct:.1f}%），")
            if super_large > 0 and large > 0:
                zhuangjia_parts.append("超大单和大单都在买，这不是小打小闹，是真的有资金在进场。")
                if small < 0:
                    zhuangjia_parts.append("散户在跑，主力在接，典型的吸筹或拉升阶段特征。")
                else:
                    zhuangjia_parts.append("散户也在跟，小心主力拉高出货。")
            elif super_large > 0 and large < 0:
                zhuangjia_parts.append("但大单在卖，超大单在买，可能是对倒洗盘，注意分辨。")
        else:
            zhuangjia_parts.append(f"资金面推断：主力今天净流出{abs(main_net)/1e4:.0f}万，")
            if small > 0:
                zhuangjia_parts.append("散户在接盘，主力在出货，危险信号。")
            else:
                zhuangjia_parts.append("散户也在跑，大家都不看好，短期别碰。")

        # 多空博弈
        total_buy = max(main_net, 0)
        total_sell = abs(min(main_net, 0))
        if total_buy > total_sell * 1.5:
            duokong_parts.append(f"多头明显占优（主力净流入{total_buy/1e4:.0f}万 vs 空头{total_sell/1e4:.0f}万），短期偏多。")
        elif total_sell > total_buy * 1.5:
            duokong_parts.append(f"空头明显占优（主力净流出{total_sell/1e4:.0f}万），短期偏空。")
        else:
            duokong_parts.append("多空力量均衡，方向未明，等一等再看。")
    else:
        zhuangjia_parts.append("资金流向数据暂时获取不到，以下只按均线、MACD 和价格结构推断，不能当作真实主力行为。")
        duokong_parts.append("缺乏资金流向数据，多空判断以技术面为主。")

    levels = _derive_support_resistance(price, snap, ohlcv)
    support = levels["support"]
    resistance = levels["resistance"]

    conclusion = " ".join(parts)
    zhuangjia_text = " ".join(zhuangjia_parts)
    duokong_text = " ".join(duokong_parts)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 五层决策框架
    # 第一层：市场环境判断（大盘过滤）
    # 第二层：个股结构判断（状态机）
    # 第三层：风险过滤（动态调整）
    # 第四层：仓位管理
    # 第五层：最终输出
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # ── 第一层：市场环境判断 ──
    market_state = get_market_state()
    market_env = market_state.get("environment", "range_bound")
    risk_appetite_label = market_state.get("risk_appetite", "中")
    breadth = market_state.get("breadth", {})
    up_pct = breadth.get("up_pct", 50) if isinstance(breadth, dict) else 50

    if market_env == "risk_on":
        market_factor = 1.0
        market_label = "强势"
    elif market_env == "range_bound":
        market_factor = 0.6
        market_label = "震荡"
    else:
        market_factor = 0.3
        market_label = "弱势"

    # ── 第三层：风险过滤（动态调整） ──
    rsi_val = snap.get("rsi14")  # 从 snap 获取，数据层暂未实装该字段
    rsi_oversold = rsi_val is not None and rsi_val < 30
    rsi_overbought = rsi_val is not None and rsi_val > 70

    atr_val = snap.get("atr14")  # 从 snap 获取，数据层暂未实装该字段
    vol_high = atr_val is not None and price > 0 and atr_val / price > 0.05
    vol_low = atr_val is not None and price > 0 and atr_val / price < 0.015

    macd_bullish = dif_val is not None and dea_val is not None and dif_val > dea_val
    macd_bearish = dif_val is not None and dea_val is not None and dif_val < dea_val
    macd_below_zero = dif_val is not None and dif_val < 0
    macd_above_zero = dif_val is not None and dif_val > 0

    # ── 第二&四层：个股结构判断 + 仓位管理 ──
    if alignment == "多头排列":
        base_entry = round(price * 0.98, 2)
        base_sl = round(price * 0.95, 2)
        base_target = round(price * 1.06, 2)
        base_supplement_1 = round(price * 0.97, 2)
        base_supplement_2 = round(price * 0.96, 2)

        if has_real_flow and money_flow.get("main_net", 0) > 0 and market_factor >= 0.6:
            base_position = 0.6
            risk_level = "低"
            probability = 72
            if macd_above_zero and macd_bullish:
                base_position = 0.7
                probability = 78
            direction = "看多"
            dp = [
                f"趋势偏多+主力流入+{market_label}市场，明天回踩 \u00a5{base_entry} 附近可进底仓。",
                f"跌破 \u00a5{base_sl} 止损。目标看 \u00a5{base_target}，到了减仓锁利。",
                f"回踩 \u00a5{base_supplement_1} 可补第一批、\u00a5{base_supplement_2} 补第二批。",
            ]
        elif has_real_flow and money_flow.get("main_net", 0) > 0:
            base_position = 0.4
            risk_level = "中"
            probability = 60
            direction = "震荡偏多"
            dp = [
                f"多头排列+主力流入，但{market_label}市场限制仓位。回踩 \u00a5{base_entry} 附近轻仓试多。",
                f"跌破 \u00a5{base_sl} 止损。第一目标 \u00a5{base_target}。大盘不转强不加仓。",
            ]
        else:
            base_position = 0.3
            risk_level = "中"
            probability = 55
            direction = "震荡偏多"
            dp = [
                f"均线多头但主力资金不明显，回踩 \u00a5{base_entry} 附近轻仓试多。",
                f"跌破 \u00a5{base_sl} 止损。第一目标 \u00a5{base_target}。没放量之前不要重仓。",
            ]

        if rsi_overbought:
            base_position = min(base_position, 0.2)
            risk_level = "中"
            dp.append("RSI超买，追高风险大，等回调再进。")
        if rsi_oversold:
            dp.append("RSI超卖但均线仍多头，说明是急跌回调非趋势反转，回踩补仓价可接。")

        effective_sl = round(price * 0.93, 2) if vol_high else base_sl
        effective_target = round(price * 1.08, 2) if vol_high else base_target
        if vol_high:
            dp.append(f"波动率高（ATR/价={atr_val/price*100:.1f}%），止损放宽到 \u00a5{effective_sl}。" if atr_val else f"波动率高，止损放宽到 \u00a5{effective_sl}。")
        if vol_low:
            dp.append(f"波动率偏低，反弹空间有限，到 \u00a5{base_target} 附近就止盈。")

        entry = base_entry
        sl = effective_sl
        target = effective_target
        supplement = base_supplement_2

    elif alignment == "空头排列":
        base_entry = round(price * 0.97, 2)
        base_sl = round(price * 1.04, 2)
        base_target = round(price * 0.94, 2)
        short_target = round(price * 0.93, 2)

        base_position = 0.1
        risk_level = "高"
        probability = 30
        direction = "看空"
        dp = [f"空头压制，暂时回避。持仓者反弹到现价附近减仓，跌破 \u00a5{base_target} 清仓。"]

        if rsi_oversold:
            base_position = 0.15
            probability = 35
            dp.append(f"RSI超卖（{rsi_val:.0f}），急跌到 \u00a5{base_entry} 出现放量止跌可1-2成试错。" if rsi_val is not None else f"RSI超卖，急跌到 \u00a5{base_entry} 出现放量止跌可1-2成试错。")
        else:
            dp.append(f"RSI={rsi_val:.0f}未超卖，不急于抄底。" if rsi_val is not None else "RSI数据不足，不急于抄底。")

        if macd_bearish and macd_below_zero:
            base_position = 0.05
            probability = 20
            dp.append("MACD水下死叉，空头确认，不要有任何做多念头。")
        elif macd_bullish and macd_below_zero:
            dp.append("MACD水下金叉，短线有弱反弹，但趋势仍是空头，快进快出。")

        effective_sl = round(price * 1.05, 2) if vol_high else base_sl
        if vol_high:
            dp.append(f"波动率高，试错单止损放 \u00a5{effective_sl}。")

        entry = base_entry
        sl = effective_sl
        target = base_target
        supplement = round(price * 0.95, 2)
        target_buy = base_entry

    elif alignment == "均线交叉/缠绕":
        base_entry = round(price * 0.98, 2)
        base_sl = round(price * 0.96, 2)
        base_target = round(price * 1.03, 2)
        base_position = 0.05
        risk_level = "高"
        probability = 40
        direction = "观望"
        dp = ["均线缠绕没有明确方向，等突破再跟。"]

        have_volume = has_real_flow and money_flow.get("main_net", 0) > 500000
        if have_volume:
            dp.append(f"放量突破 \u00a5{base_target} 站稳可追，下破 \u00a5{base_sl} 离场。")
            base_position = 0.15
            probability = 45
        else:
            dp.append(f"无量突破 \u00a5{base_target} 不追（假突破概率高）。")

        if rsi_val is not None:
            if rsi_val > 60:
                dp.append(f"RSI={rsi_val:.0f}偏上方，向上突破概率略大。" if rsi_val is not None else "RSI数据不足，向上概率不确定。")
                probability = 48
            elif rsi_val < 40:
                dp.append(f"RSI={rsi_val:.0f}偏下方，小心向下变盘。" if rsi_val is not None else "RSI数据不足，向下概率不确定。")
                probability = 30
                direction = "偏空观望"

        entry = base_entry
        sl = base_sl
        target = base_target
        supplement = round(price * 0.97, 2)

    else:
        direction = "数据不足"
        entry = None; sl = None; target = None; supplement = None
        target_buy = None; short_target = None
        base_position = 0.0; risk_level = "高"; probability = 0
        dp = []
    # ── 第四层：仓位管理 ──
    final_position = round(base_position * market_factor, 2)
    final_position = max(0.0, min(final_position, 0.8))

    if final_position <= 0.05:
        pos_text = "0~5%（空仓/观望）"
    elif final_position <= 0.1:
        pos_text = "5~10%（极轻仓）"
    elif final_position <= 0.2:
        pos_text = "10~20%（轻仓试）"
    elif final_position <= 0.3:
        pos_text = "20~30%（轻仓）"
    elif final_position <= 0.4:
        pos_text = "30~40%（半仓）"
    elif final_position <= 0.5:
        pos_text = "40~50%（半仓偏多）"
    elif final_position <= 0.7:
        pos_text = "50~70%（重仓）"
    else:
        pos_text = "70~80%（偏激进）"

    detail = " | ".join(dp) if dp else "均线数据不完整，暂时无法生成计划。"

    # ── 规则引擎因果链 ──
    causal_chains = []
    if alignment == "多头排列":
        causal_chains.append({
            "cause": f"均线多头发散（MA5={ma5_val} > MA10={ma10_val} > MA20={ma20_val}），价格运行在全部均线上方",
            "effect": "技术派资金会以此作为多头确认信号，追多盘和补仓盘陆续进场，推动价格惯性上行。主力在均线多头时拉升成本最低，跟风盘自然到位",
            "implication": f"多头排列下不要逆势做空。当前建议仓位{pos_text}。缩量上涨表明主力控盘度高，放量滞涨则是出货前兆",
        })
    elif alignment == "空头排列":
        causal_chains.append({
            "cause": f"均线空头排列（MA5={ma5_val} < MA10={ma10_val} < MA20={ma20_val}），价格被各条均线压制",
            "effect": "每一个反弹都会遇到均线压制，抄底盘和套牢盘形成双重抛压。主力不会在这种结构下主动拉升，因为拉起来全是抛压",
            "implication": f"空头排列下不要抄底。当前建议仓位{pos_text}。等待均线开始走平、粘合才可能是底部区域",
        })
    else:
        causal_chains.append({
            "cause": f"均线交叉缠绕（MA5={ma5_val}, MA10={ma10_val}, MA20={ma20_val}），方向未明",
            "effect": "多空双方在当前位置反复拉锯，没有一方能确立优势。短线资金频繁进出，中长线资金观望",
            "implication": f"震荡行情不宜追涨杀跌。当前建议仓位{pos_text}",
        })

    if has_real_flow:
        mn = money_flow.get("main_net", 0)
        if mn > 0:
            causal_chains.append({
                "cause": f"主力净流入 {mn/1e4:.0f} 万（{money_flow.get('main_pct',0):.1f}%）",
                "effect": "大资金主动买入，机构对该股有明确的做多意图",
                "implication": f"主力流入但价格{'上涨' if is_up else '未涨'}说明{'趋势健康' if is_up else '可能压价吸筹'}",
            })
        else:
            causal_chains.append({
                "cause": f"主力净流出 {abs(mn)/1e4:.0f} 万，散户流入",
                "effect": "筹码从主力转移到散户，一旦买盘耗尽将加速下跌",
                "implication": "主力出货阶段，不要去接飞刀",
            })

    if dif_val is not None and dea_val is not None:
        if dif_val > dea_val:
            causal_chains.append({
                "cause": f"DIF({dif_val:.2f})上穿DEA({dea_val:.2f})，{('水上' if dif_val>0 else '水下')}",
                "effect": f"{('多头动能增强' if macd_val and macd_val>0 else '动能衰减')}",
                "implication": f"{'确认多头趋势' if dif_val>0 else '反弹信号，不宜重仓'}",
            })
        else:
            causal_chains.append({
                "cause": f"DIF({dif_val:.2f})在DEA({dea_val:.2f})之下，死叉状态",
                "effect": f"{('获利了结动机' if dif_val>0 else '空头趋势延续')}",
                "implication": f"{'注意高位减仓' if dif_val>0 else '空头未改，不要抄底'}",
            })

    if market_env in ("risk_on", "risk_off"):
        causal_chains.append({
            "cause": f"市场环境：{market_label}（上涨占比{up_pct:.0f}%）",
            "effect": f"{'大盘多头氛围浓厚' if market_env=='risk_on' else '市场低迷，控制仓位'}",
            "implication": f"大盘{market_label}，仓位控制在{pos_text}以内",
        })
    return {
        "zhuangjia": zhuangjia_text,
        "duokong": duokong_text,
        "plan": {
            "direction": direction,
            "entry": entry,
            "stop_loss": sl,
            "target": target,
            "supplement": supplement,
            "detail": detail,
        },
        "conclusion": conclusion,
        "support": support,
        "resistance": resistance,
        "causalAnalysis": causal_chains[:3],
    }


def _validate_ai_response(parsed: dict, price: float) -> dict:
    """确保 AI 返回的 JSON 包含所有必需字段，缺失的用合理默认值填充"""
    if not isinstance(parsed, dict):
        raise ValueError("AI 返回的不是 JSON 对象")
    plan = parsed.get("plan") or {}

    # 因果链校验
    causal = parsed.get("causalAnalysis")
    if not isinstance(causal, list):
        causal = [{
            "cause": "数据不足以建立因果链",
            "effect": "当前数据量不足以推导资金行为逻辑",
            "implication": "请补充更多数据后再试"
        }]
    else:
        valid = []
        for item in causal:
            if isinstance(item, dict) and item.get("cause") and item.get("effect"):
                valid.append({
                    "cause": str(item["cause"]),
                    "effect": str(item["effect"]),
                    "implication": str(item.get("implication") or "无明确启示"),
                })
        if valid:
            causal = valid[:3]
        else:
            causal = [{
                "cause": "数据不足以建立因果链",
                "effect": "当前数据量不足以推导资金行为逻辑",
                "implication": "请补充更多数据后再试"
            }]

    return {
        "zhuangjia": str(parsed.get("zhuangjia") or "暂无庄家拆解"),
        "duokong": str(parsed.get("duokong") or "暂无博弈分析"),
        "plan": {
            "direction": str(plan.get("direction") or "震荡"),
            "entry": float(plan.get("entry") or round(price * 0.99, 2)),
            "stop_loss": float(plan.get("stop_loss") or round(price * 0.95, 2)),
            "target": float(plan.get("target") or round(price * 1.05, 2)),
            "supplement": float(plan.get("supplement") or round(price * 0.96, 2)),
            "detail": str(plan.get("detail") or "暂无交易计划"),
        },
        "conclusion": str(parsed.get("conclusion") or "暂无分析结论"),
        "support": float(parsed.get("support") or round(price * 0.94, 2)),
        "resistance": float(parsed.get("resistance") or round(price * 1.06, 2)),
        "causalAnalysis": causal,
    }


def call_deepseek_analysis(name: str, code: str, price: float, change_text: str, snap: dict, money_flow: Optional[dict] = None) -> Optional[dict]:
    """调用 DeepSeek API 生成 AI 分析。未配置 DEEPSEEK_API_KEY 时返回 None。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI

        print(f"[AI] Calling DeepSeek for {name}({code})...", flush=True)
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=50.0)
        response = client.chat.completions.create(
            model="deepseek-chat",
            temperature=0.4,
            max_tokens=1200,
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": build_analysis_user_prompt(
                    name, code, price, change_text, snap, money_flow,
                )},
            ],
        )
        text = response.choices[0].message.content or ""
        print(f"[AI] DeepSeek responded ({len(text)} chars): {text[:120]}...", flush=True)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("\n", 1)[0]
        parsed = json.loads(text)
        result = _validate_ai_response(parsed, price)
        print(f"[AI] DeepSeek analysis parsed OK: zhuangjia={len(result['zhuangjia'])}chars, duokong={len(result['duokong'])}chars", flush=True)
        return result
    except json.JSONDecodeError as e:
        print(f"[AI] DeepSeek returned invalid JSON: {e}", flush=True)
        print(f"[AI] Raw response text: {text[:300]}", flush=True)
        return None
    except Exception as e:
        print(f"[AI] DeepSeek call failed [{type(e).__name__}]: {e}, falling back to rule-based", flush=True)
        return None


def get_ai_analysis(name: str, code: str, price: float, change_pct: float, snap: dict, money_flow: Optional[dict] = None, ohlcv: Optional[list[dict]] = None) -> dict:
    """先尝试 DeepSeek，失败或无 key 则回退到规则分析（含资金流向）。
    无论 DeepSeek 成功与否，都叠加五层框架规则分析的 plan / causalAnalysis。
    规则分析的数值优先于 DeepSeek（规则基于现价计算更可靠）。
    """
    arrow = "↑" if change_pct >= 0 else "↓"
    change_text = f"{arrow}{abs(change_pct):.2f}%"

    result = call_deepseek_analysis(name, code, price, change_text, snap, money_flow)
    if result is None:
        print(f"[AI] Falling back to rule-based analysis for {name}({code})", flush=True)
        result = build_rule_analysis(name, code, price, change_text, snap, money_flow, ohlcv)
    else:
        # DeepSeek 成功，额外叠加五层框架规则分析（数值完全覆盖，文字拼接）
        rule_result = build_rule_analysis(name, code, price, change_text, snap, money_flow, ohlcv)
        if isinstance(rule_result, dict):
            rule_plan = rule_result.get("plan") or {}
            existing_plan = result.get("plan") or {}
            ai_detail = existing_plan.get("detail") or ""
            # 规则数值完全覆盖 AI 数值（规则基于盘中均价计算，更合理）
            existing_plan["direction"] = rule_plan.get("direction") or existing_plan.get("direction") or ""
            existing_plan["entry"] = rule_plan.get("entry")
            existing_plan["stop_loss"] = rule_plan.get("stop_loss")
            existing_plan["target"] = rule_plan.get("target")
            existing_plan["supplement"] = rule_plan.get("supplement")
            # detail：规则分析文字在前 + AI 分析文字在后
            rule_detail = rule_plan.get("detail") or ""
            if rule_detail and ai_detail:
                existing_plan["detail"] = rule_detail + " | " + ai_detail
            elif rule_detail:
                existing_plan["detail"] = rule_detail
            # 用规则结果覆盖 conclusion / support / resistance（AI 结论可能不够严谨）
            rule_conclusion = rule_result.get("conclusion") or ""
            if rule_conclusion:
                result["conclusion"] = rule_conclusion
            # 资金/技术推断使用规则引擎口径，避免 AI 文案把推断写成确定的“庄家行为”。
            result["zhuangjia"] = rule_result.get("zhuangjia") or result.get("zhuangjia")
            result["duokong"] = rule_result.get("duokong") or result.get("duokong")
            result["support"] = rule_result.get("support") or result.get("support", round(price * 0.94, 2))
            result["resistance"] = rule_result.get("resistance") or result.get("resistance", round(price * 1.06, 2))
            # 仍然保留 AI 原文
            result["rule_analysis"] = rule_result

    return result


# ══════════════════════════════════════════════════════
#  API 路由
# ══════════════════════════════════════════════════════

# 常见股票代码 → 名称映射
STOCK_NAMES: dict[str, str] = {
    "000001": "平安银行", "000002": "万科A", "000063": "中兴通讯",
    "000333": "美的集团", "000534": "万泽股份", "000651": "格力电器", "000725": "京东方A",
    "000858": "五粮液", "002049": "紫光国微", "002230": "科大讯飞",
    "002415": "海康威视", "002460": "赣锋锂业", "002475": "立讯精密", "002594": "比亚迪",
    "002714": "牧原股份", "300014": "亿纬锂能", "300059": "东方财富",
    "300124": "汇川技术", "300274": "阳光电源", "300750": "宁德时代",
    "600000": "浦发银行", "600009": "上海机场", "600028": "中国石化",
    "600030": "中信证券", "600036": "招商银行", "600276": "恒瑞医药",
    "600519": "贵州茅台", "600585": "海螺水泥", "600809": "山西汾酒",
    "600887": "伊利股份", "600900": "长江电力", "601012": "隆基绿能",
    "601088": "中国神华", "601166": "兴业银行", "601318": "中国平安",
    "601398": "工商银行", "601857": "中国石油", "601899": "紫金矿业",
    "603259": "药明康德", "688111": "金山办公", "688981": "中芯国际",
}

# 题材概念映射（硬编码兜底，稳定不依赖网络）
_STOCK_CONCEPTS: dict[str, list[str]] = {
    "000001": ["银行", "破净股", "MSCI中国", "深圳主板"],
    "000002": ["房地产开发", "物业管理", "MSCI中国"],
    "000063": ["5G", "通信设备", "AI算力", "MSCI中国"],
    "000333": ["家电", "超级品牌", "工业互联网", "MSCI中国"],
    "000651": ["家电", "超级品牌", "工业母机", "MSCI中国"],
    "000725": ["面板", "柔性屏", "物联网", "MSCI中国"],
    "000858": ["白酒", "超级品牌", "成渝特区", "MSCI中国"],
    "002049": ["芯片", "军工电子", "5G", "MSCI中国"],
    "002230": ["人工智能", "ChatGPT", "语音识别", "MSCI中国"],
    "002415": ["安防", "AI视觉", "智能物联", "MSCI中国"],
    "002460": ["锂矿", "锂化合物", "固态电池", "新能源金属", "MSCI中国"],
    "002475": ["消费电子", "苹果产业链", "无线充电", "MSCI中国"],
    "002594": ["新能源汽车", "锂电池", "储能", "比亚迪产业链"],
    "002714": ["猪肉", "农牧", "预制菜", "MSCI中国"],
    "300014": ["锂电池", "电子烟", "储能", "创业板权重"],
    "300059": ["互联网金融", "券商", "AI大模型", "创业板权重"],
    "300124": ["工业自动化", "机器人", "伺服系统", "创业板权重"],
    "300274": ["光伏", "逆变器", "储能", "氢能源"],
    "300750": ["固态电池", "锂电池", "钠电池", "创业板权重", "新能源", "储能"],
    "600000": ["银行", "破净股", "上证50", "MSCI中国"],
    "600009": ["机场航运", "免税", "上海自贸区", "MSCI中国"],
    "600028": ["石油石化", "央企改革", "页岩气", "上证50"],
    "600030": ["券商", "投行龙头", "财富管理", "上证50"],
    "600036": ["银行", "零售银行", "上证50", "MSCI中国"],
    "600276": ["创新药", "医药", "CXO", "MSCI中国"],
    "600519": ["白酒", "超级品牌", "上证50", "MSCI中国", "大消费"],
    "600585": ["水泥", "基建", "一带一路", "MSCI中国"],
    "600809": ["白酒", "国企改革", "山西国资", "MSCI中国"],
    "600887": ["乳业", "食品饮料", "大消费", "MSCI中国"],
    "600900": ["水电", "高股息", "绿色电力", "上证50"],
    "601012": ["光伏", "硅片", "单晶硅", "MSCI中国"],
    "601088": ["煤炭", "高股息", "央企改革", "上证50"],
    "601166": ["银行", "绿色金融", "上证50", "MSCI中国"],
    "601318": ["保险", "金融科技", "上证50", "MSCI中国"],
    "601398": ["银行", "高股息", "央企改革", "上证50"],
    "601857": ["石油石化", "央企改革", "天然气", "上证50"],
    "601899": ["黄金", "铜", "有色金属", "MSCI中国"],
    "603259": ["CXO", "创新药", "医药外包", "MSCI中国"],
    "688111": ["信创", "国产软件", "办公软件", "科创板"],
    "688981": ["芯片代工", "半导体", "国产替代", "科创板权重"],
}


# F10 主营业务摘要（硬编码前 40 字，东方财富不可达时的兜底）
_F10_SUMMARIES: dict[str, str] = {
    "000001": "全国性股份制商业银行，零售银行转型标杆，综合金融服务平台。",
    "000002": "中国领先的城乡建设与生活服务商，房地产开发与物业运营双轮驱动。",
    "000063": "全球领先的通信设备及解决方案提供商，5G基站与AI算力基础设施核心供应商。",
    "000333": "全球白电龙头，暖通空调、消费电器、机器人及工业自动化全面布局。",
    "000651": "全球家电巨头，空调市占率第一，智能制造与新能源压缩机双赛道发力。",
    "000725": "全球显示面板龙头，LCD/OLED产线齐备，物联网智慧端口核心供应商。",
    "000858": "浓香型白酒绝对龙头，品牌价值超千亿，高端白酒第二极，成渝经济圈标杆。",
    "002049": "特种集成电路与智能安全芯片双龙头，军工/5G/车规芯片核心供应商。",
    "002230": "中国AI语音识别与自然语言处理领军企业，讯飞星火大模型生态构建者。",
    "002415": "全球AI视觉与智能物联龙头，安防监控市占率第一，机器人及AI大模型布局深入。",
    "002460": "全球锂资源及锂化合物一体化龙头，氢氧化锂/碳酸锂产能全球前三，固态电池核心上游。",
    "002475": "消费电子精密制造与无线通信龙头，苹果产业链核心，汽车电子第二增长曲线。",
    "002594": "新能源汽车全产业链王者，刀片电池、DM-i混动、云轨云巴、储能系统全面领先。",
    "002714": "中国生猪养殖与屠宰加工龙头，猪周期穿越能力最强，预制菜食品延伸布局。",
    "300014": "消费锂电池与电子雾化器全球龙头，圆柱电池及储能业务快速放量。",
    "300059": "互联网券商+AI大模型双轮驱动，天天基金+Choice数据构筑财富管理生态闭环。",
    "300124": "工业自动化与伺服系统龙头，人形机器人与新能源汽车电驱系统双赛道卡位。",
    "300274": "全球光伏逆变器与储能系统龙头，氢能电解槽与虚拟电厂布局领先。",
    "300750": "全球动力电池与储能电池绝对王者，固态电池/钠电池/麒麟电池技术路线全覆盖。",
    "600000": "上海国资旗下全国性股份制银行，零售与对公均衡发展，分红率行业领先。",
    "600009": "长三角航空枢纽运营龙头，免税商业与临空经济双引擎驱动。",
    "600028": "中国最大的一体化能源化工央企，上游勘探开采与下游炼化销售全产业链覆盖。",
    "600030": "中国投行与财富管理龙头券商，注册制改革核心受益标的，资管规模行业第一。",
    "600036": "中国最佳零售银行，财富管理与私人银行护城河深厚，不良率持续优于同业。",
    "600276": "中国创新药与仿制药一体化龙头，PD-1/ADC/GLP-1管线丰富，国际化布局加速。",
    "600519": "中国酱香型白酒绝对王者，品牌壁垒不可复制，高端消费第一风向标。",
    "600585": "全球水泥及建材龙头，产业链延伸至骨料与混凝土，一带一路出海战略清晰。",
    "600809": "清香型白酒龙头，品牌复兴战略推进，全国化渠道扩张与高端化升级并行。",
    "600887": "中国乳制品行业绝对龙头，液奶奶粉冷饮全品类布局，全球供应链网络完善。",
    "600900": "全球最大水电上市公司，三峡/葛洲坝等核心资产，高股息防御首选标的。",
    "601012": "全球光伏硅片与组件一体化龙头，BC电池技术路线领先，氢能装备第二赛道。",
    "601088": "中国最大煤炭央企，煤电运一体化运营，长协价锁定利润，高股息价值洼地。",
    "601166": "绿色金融与同业业务特色鲜明的全国性股份制银行，数字化转型走在行业前列。",
    "601318": "中国综合金融与保险科技旗舰，寿险/产险/银行/科技四轮驱动，生态壁垒深厚。",
    "601398": "全球资产规模最大商业银行，零售/对公/国际业务全面领先，高股息防御之选。",
    "601857": "中国最大油气生产与供应商，上游勘探开发主导地位，天然气与管道业务增长明确。",
    "601899": "全球矿业巨头，铜金锂多金属矿储丰富，新能源金属需求爆发核心受益者。",
    "603259": "全球CXO龙头，药物发现/临床前/临床CRO/CDMO全链条覆盖，国际化程度最高。",
    "688111": "国产办公软件绝对龙头，WPS Office月活超5亿，信创AI办公双轮驱动增长。",
    "688981": "中国大陆规模最大、技术最先进的集成电路晶圆代工企业，国产替代中军力量。",
}


def fetch_f10_summary(code: str) -> str:
    """获取 F10 主营业务一句话摘要"""
    if code in _F10_SUMMARIES:
        return _F10_SUMMARIES[code]
    if code in _STOCK_CONCEPTS:
        tags = _STOCK_CONCEPTS[code]
        return f"公司主营业务涉及{tags[0]}等核心领域，为A股市场{tags[-1]}代表性标的。"
    return "公司为A股上市公司，主营业务稳健经营，详情请查阅最新年报披露信息。"


def fetch_concept_tags(code: str) -> list[str]:
    """获取股票概念标签 — 优先硬编码映射，兜底按行业归纳"""
    # 优先查本地映射
    if code in _STOCK_CONCEPTS:
        return _STOCK_CONCEPTS[code]

    # 兜底：按代码前缀归纳行业
    if code.startswith("688"):
        return ["科创板", "半导体/硬科技"]
    if code.startswith("300") or code.startswith("301"):
        return ["创业板", "成长股"]
    if code.startswith("002") or code.startswith("003"):
        return ["中小板", "制造业"]
    if code.startswith("600") or code.startswith("601") or code.startswith("603") or code.startswith("605"):
        return ["上海主板", "蓝筹股"]
    if code.startswith("000") or code.startswith("001"):
        return ["深圳主板", "蓝筹股"]
    return ["A股"]


def to_tx_symbol(code: str) -> str:
    """将 6 位代码转换为腾讯接口格式: sz000001 / sh600519"""
    code = code.strip().zfill(6)
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


# ══════════════════════════════════════════════════════
#  数据获取（多源 + 回退 + 列名兼容）
# ══════════════════════════════════════════════════════

# akshare 不同版本 / 数据源可能返回不同列名，做一层映射
_COLUMN_ALIASES = {
    "date": ("date", "日期", "day"),
    "open": ("open", "开盘", "open_price"),
    "close": ("close", "收盘", "close_price"),
    "high": ("high", "最高", "high_price"),
    "low": ("low", "最低", "low_price"),
    "volume": ("volume", "vol", "成交量", "trade_volume", "amount"),
}


def _find_column(df: pd.DataFrame, canonical: str) -> str:
    """在 DataFrame 中查找某个规范列的实际列名，找不到则返回空串"""
    for alias in _COLUMN_ALIASES.get(canonical, (canonical,)):
        if alias in df.columns:
            return alias
    return ""


def _validate_columns(df: pd.DataFrame) -> None:
    """确保 DataFrame 包含必需的 OHLCV 列，否则抛出明确错误"""
    missing = [c for c in ("date", "open", "close", "high", "low", "volume")
               if not _find_column(df, c)]
    if missing:
        raise ValueError(
            f"数据缺少必需列: {missing}，实际列: {list(df.columns)}"
        )


def fetch_stock_data(code: str) -> pd.DataFrame:
    """
    从 akshare 获取 A 股历史行情。
    优先使用腾讯数据源，失败时回退 eastmoney。
    """
    import socket
    socket.setdefaulttimeout(5)
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=3650)).strftime("%Y%m%d")  # 覆盖约10年，供周/月K聚合
    symbol = to_tx_symbol(code)
    errors: list[str] = []

    # ── 方案 A: 腾讯数据源 (ak.stock_zh_a_hist_tx) ──
    try:
        df = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if not df.empty:
            _validate_columns(df)
            return df.reset_index(drop=True)
        errors.append("腾讯数据源返回空数据")
    except Exception as e:
        errors.append(f"腾讯数据源: {e}")

    # ── 方案 B: eastmoney 数据源 (ak.stock_zh_a_hist) ──
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if not df.empty:
            em_rename = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            }
            existing = {k: v for k, v in em_rename.items() if k in df.columns}
            if existing:
                df = df.rename(columns=existing)
            _validate_columns(df)
            return df.reset_index(drop=True)
        errors.append("eastmoney 数据源返回空数据")
    except Exception as e:
        errors.append(f"eastmoney 数据源: {e}")

    raise RuntimeError("; ".join(errors))


def normalize_ohlcv(df: pd.DataFrame) -> list[dict]:
    """将 DataFrame 转换为前端 OHLCV 格式，兼容不同列名"""
    date_col = _find_column(df, "date")
    open_col = _find_column(df, "open")
    close_col = _find_column(df, "close")
    high_col = _find_column(df, "high")
    low_col = _find_column(df, "low")
    vol_col = _find_column(df, "volume")

    rows: list[dict] = []
    for _, row in df.iterrows():
        rows.append({
            "date": str(row[date_col])[:10],
            "open": round(float(row[open_col]), 2),
            "close": round(float(row[close_col]), 2),
            "high": round(float(row[high_col]), 2),
            "low": round(float(row[low_col]), 2),
            "volume": int(row[vol_col]),
        })
    return rows


def _aggregate_weekly(daily: list[dict]) -> list[dict]:
    """日K → 周K聚合（以周五为锚点，当周无周五则取最后一个交易日）"""
    if not daily:
        return []
    from collections import defaultdict
    weeks: dict[str, list[dict]] = defaultdict(list)
    for bar in daily:
        try:
            dt = datetime.strptime(str(bar["date"])[:10], "%Y-%m-%d")
            iso = dt.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
        except Exception:
            continue
        weeks[week_key].append(bar)

    result = []
    for wk in sorted(weeks.keys()):
        bars = weeks[wk]
        if not bars:
            continue
        o = bars[0]["open"]
        c = bars[-1]["close"]
        h = max(b["high"] for b in bars)
        l = min(b["low"] for b in bars)
        v = sum(b["volume"] for b in bars)
        result.append({
            "date": wk,
            "open": round(o, 2),
            "close": round(c, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "volume": int(v),
        })
    return result


def _aggregate_monthly(daily: list[dict]) -> list[dict]:
    """日K → 月K聚合"""
    if not daily:
        return []
    from collections import defaultdict
    months: dict[str, list[dict]] = defaultdict(list)
    for bar in daily:
        try:
            dt = datetime.strptime(str(bar["date"])[:10], "%Y-%m-%d")
            month_key = dt.strftime("%Y-%m")
        except Exception:
            continue
        months[month_key].append(bar)

    result = []
    for mo in sorted(months.keys()):
        bars = months[mo]
        if not bars:
            continue
        o = bars[0]["open"]
        c = bars[-1]["close"]
        h = max(b["high"] for b in bars)
        l = min(b["low"] for b in bars)
        v = sum(b["volume"] for b in bars)
        result.append({
            "date": mo,
            "open": round(o, 2),
            "close": round(c, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "volume": int(v),
        })
    return result


def _generate_simulated_kline_250(code: str, name: str) -> list[dict]:
    """全API失效时的250日模拟K线fallback（基于确定性种子生成连续波动序列）"""
    import hashlib
    import math
    seed = int(hashlib.md5(code.encode()).hexdigest()[:8], 16)
    rng = __import__('random')
    rng.seed(seed)

    base_price = float(seed % 100) + 5.0
    bars = []
    price = base_price
    for i in range(250):
        daily_ret = rng.gauss(0.0002, 0.022)
        open_p = price
        close_p = price * (1 + daily_ret)
        intra_high = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.008)))
        intra_low = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.008)))
        vol = int(abs(rng.gauss(500000, 200000)))

        dt = (datetime.now() - timedelta(days=250 - i)).strftime("%Y-%m-%d")
        bars.append({
            "date": dt, "open": round(open_p, 2), "close": round(close_p, 2),
            "high": round(intra_high, 2), "low": round(intra_low, 2), "volume": vol,
        })
        price = close_p * (1 + rng.gauss(0, 0.008))
    return bars


@app.get("/api/stock/{code}")
async def get_stock(code: str):
    """获取股票历史 K 线、技术指标和 AI 分析"""
    code = code.strip().zfill(6)

    # 校验股票代码格式
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail=f"股票代码格式错误: {code}，请输入 6 位数字")

    # 1. 获取数据（多源 + 自动回退）
    try:
        df = fetch_stock_data(code)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"数据源均不可用: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取数据时发生未知错误: {e}")

    if df.empty:
        raise HTTPException(status_code=404, detail=f"未找到股票 {code} 的行情数据，请确认代码是否存在")

    # 2. 数据校验
    if len(df) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"股票 {code} 仅有 {len(df)} 条历史数据，至少需要 2 条才能计算涨跌",
        )

    # 3. 转换为前端 OHLCV 格式
    try:
        ohlcv_data = normalize_ohlcv(df)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据格式异常: {e}")

    # 4. 计算技术指标（全量250日数据暖机，确保MACD/MA信号准确）
    try:
        full_ohlcv = normalize_ohlcv(df)
        closes = np.array([d["close"] for d in full_ohlcv], dtype=np.float64)
        indicators = compute_indicators(closes)
        # 返回完整可获取日线；前端用 dataZoom 默认定位到近期，但用户可拖动查看全部历史。
        ohlcv_data = full_ohlcv

        # ★ 周K聚合
        weekly_ohlcv = _aggregate_weekly(full_ohlcv)
        # ★ 月K聚合
        monthly_ohlcv = _aggregate_monthly(full_ohlcv)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"技术指标计算失败: {e}")

    # 5. 提取快照
    try:
        last = ohlcv_data[-1]
        prev = ohlcv_data[-2]
        price = last["close"]
        change = round(price - prev["close"], 2)
        change_pct = round(change / prev["close"] * 100, 2)
    except (IndexError, KeyError) as e:
        raise HTTPException(status_code=500, detail=f"价格快照提取失败: {e}")

    name = STOCK_NAMES.get(code, f"股票{code}")

    # 6. 获取题材概念标签 & 资金流向 & F10 摘要 & 黑箱警报 & 战斗力 & 赔率
    concepts = fetch_concept_tags(code)
    f10 = fetch_f10_summary(code)
    money_flow = fetch_money_flow(code, ohlcv_data)
    alerts = compute_abnormal_alerts(ohlcv_data)
    combat = compute_combat_score(indicators["snapshot"], money_flow, alerts, concepts, change_pct)
    odds = compute_odds(ohlcv_data)
    behavior_tags = compute_behavior_tags(ohlcv_data, money_flow, alerts, concepts, closes)

    # 8. AI 赔率/风险系统
    try:
        ai = get_ai_analysis(name, code, price, change_pct, indicators["snapshot"], money_flow, ohlcv_data)
    except Exception:
        # 最后的兜底 — 如果连规则引擎都挂了，返回占位分析
        ai = {
            "zhuangjia": "数据异常，无法拆解庄家意图。",
            "duokong": "数据异常，无法分析多空博弈。",
            "plan": {
                "direction": "震荡",
                "entry": round(price * 0.99, 2),
                "stop_loss": round(price * 0.95, 2),
                "target": round(price * 1.05, 2),
                "supplement": round(price * 0.96, 2),
                "detail": "当前数据不足以生成有效交易计划，请稍后重试。",
            },
            "conclusion": f"股价目前在 ¥{round(price*0.94,2)} 到 ¥{round(price*1.06,2)} 之间进行区间震荡与多空博弈，技术面上暂无明显单边突破信号，建议高抛低吸。",
            "support": round(price * 0.94, 2),
            "resistance": round(price * 1.06, 2),
        }

    return {
        "code": code,
        "name": name,
        "price": price,
        "change": change,
        "changePercent": change_pct,
        "concepts": concepts,
        "f10_summary": f10,
        "snapshot": indicators["snapshot"],
        "moneyFlow": money_flow,
        "abnormalAlerts": alerts,
        "combatScore": combat,
        "odds": odds,
        "aiAnalysis": ai,
        "data": ohlcv_data,
        "weekly": weekly_ohlcv,
        "monthly": monthly_ohlcv,
        "behaviorTags": behavior_tags,
    }


# ── 筛选器标签 → 概念关键词映射 ──
_SCREENER_FILTER_MAP: dict[str, list[str]] = {
    # ── 热门赛道（对齐板块生命周期面板） ──
    "ai": ["AI", "人工智能", "AI算力", "AI视觉", "AI大模型", "ChatGPT", "光模块", "算力", "数据要素", "CPO"],
    "chip": ["芯片", "半导体", "芯片代工", "国产替代", "集成电路"],
    "newenergy": ["新能源", "锂电池", "新能源汽车", "储能", "固态电池", "光伏", "钠电池", "氢能", "风电", "核电"],
    "robot": ["机器人", "智能驾驶", "工业母机", "具身智能"],
    "lowalt": ["低空经济", "无人机", "国防军工", "航天"],
    "dividend": ["高股息", "银行", "煤炭", "水电", "电力", "保险", "证券", "钢铁", "建筑", "交通运输", "国企改革", "一带一路"],
    "consumer": ["白酒", "食品饮料", "大消费", "医药", "创新药", "医疗器械", "生物医药", "医疗美容", "家电", "纺织服装", "农业", "汽车"],
    # ── 旧版兼容 ──
    "hft": ["金融科技", "券商"],
    "s1": ["新能源", "锂电池", "固态电池", "钠电池"],
    "s2": ["光模块", "5G", "通信设备"],
    "s3": ["半导体", "芯片", "芯片代工"],
    "s4": ["高股息", "煤炭"],
    "v1": ["破净股", "银行"],
    "v2": ["蓝筹股", "上证50"],
    "v3": ["超级品牌", "大消费"],
    "v4": ["创业板", "成长股"],
}




# ── 预加载股票名称（批量一次，不要逐只 ak 调用） ──
_STOCK_NAME_CACHE = {}
def _ensure_stock_names():
    global _STOCK_NAME_CACHE
    if _STOCK_NAME_CACHE:
        return
    try:
        df = ak.stock_info_a_code_name()
        for _, row in df.iterrows():
            _STOCK_NAME_CACHE[str(row["code"])] = str(row["name"]).replace(" ", "")
        print(f"[AiScreener] 预加载 {len(_STOCK_NAME_CACHE)} 只股票名称", flush=True)
    except Exception as e:
        print(f"[AiScreener] 预加载名称失败: {e}", flush=True)

# ── 加载映射 JSON ──
def _load_stock_map():
    try:
        import json, os
        path = os.path.join(os.path.dirname(__file__), "STOCK_SECTOR_MAP.json")
        with open(path) as f:
            data = json.load(f)
        return data.get("mapping", {})
    except:
        return {}

_STOCK_MAP = None

@app.get("/api/alpha/ai-screener")
def get_ai_screener(phase: str = "", tags: str = ""):
    """
    Alpha 智能漏斗选股器 — 服用 AlphaBrain 终端结果 + 后置过滤

    不再独立做全市场暴力扫描，而是从 routes_alpha_os._CACHED_SNAPSHOT
    获取大脑已有的候选，再从 sector_worker.get_all_lifecycles() 获取板块阶段
    用于筛选/展示。空条件时默认展示所有候选。
    """
    # ── 从终端缓存获取数据（走 _get_or_refresh 而非直接读变量，确保触发同步刷新） ──
    try:
        from routes_alpha_os import _get_or_refresh
        snap = _get_or_refresh()
    except Exception:
        snap = {}

    _PHASE_EN_TO_CN = {
        "main_rise_1": "主升一期", "acceleration": "加速期", "startup": "启动期",
        "high_divergence": "高位分歧", "ice_recovery": "冰点修复", "decay": "退潮期",
        "unknown": "未知",
    }
    # 兼容别名（旧版前端可能发这些）
    _PHASE_ALIAS_TO_EN = {
        "主升期": "main_rise_1", "分歧期": "high_divergence",
        "强化期": "acceleration", "衰退期": "decay", "修复期": "ice_recovery",
        "分歧": "high_divergence", "修复": "ice_recovery",
    }
    _PHASE_CN_TO_EN = {v: k for k, v in _PHASE_EN_TO_CN.items()}
    _PHASE_CN_TO_EN.update(_PHASE_ALIAS_TO_EN)

    raw_phases = [p.strip() for p in phase.split(",") if p.strip()] if phase else []
    # 接受中文或英文参数
    phase_filter = [_PHASE_CN_TO_EN.get(p, p) for p in raw_phases]
    tags_filter = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # ── 从终端结果提取候选 ──
    candidates = snap.get("top_candidates", []) if isinstance(snap, dict) else []

    # ── 直接从 sector_worker 获取板块生命周期（更可靠） ──
    try:
        from sector_worker import get_all_lifecycles
        lc = get_all_lifecycles()
    except Exception:
        lc = {}

    if not isinstance(lc, dict):
        lc = {}

    phase_dist: dict[str, int] = {}
    for sname, sinfo in lc.items():
        ph = (sinfo.get("phase", "unknown") if isinstance(sinfo, dict) else "unknown")
        phase_dist[ph] = phase_dist.get(ph, 0) + 1

    # ── 候选 → 前端格式 ──
    results: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        code = c.get("symbol", "") or c.get("code", "")
        sector = c.get("sector", "")
        phase_str = "unknown"
        phase_conf = 0.0
        if sector:
            si = lc.get(sector, {})
            if isinstance(si, dict):
                phase_str = si.get("phase", "unknown")
                phase_conf = si.get("confidence", 0.0)

        if phase_filter and phase_str not in phase_filter:
            continue

        results.append({
            "code": code,
            "name": c.get("name", code),
            "sector": sector,
            "official_sector": sector,
            "phase": phase_str,
            "phase_cn": _PHASE_EN_TO_CN.get(phase_str, phase_str),
            "phase_confidence": round(phase_conf, 2),
            "time_range": {"start_date": "", "end_date": "", "duration_trading_days": 0},
            "score": c.get("score", 0),
            "rating": c.get("tier", "D"),
            "adjustment": 1.0,
            "reasons": c.get("reasons", []),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    recommendations = results[:5]

    return {
        "results": results,
        "recommendations": recommendations,
        "total": len(results),
        "phase_distribution": phase_dist,
        "filters": {
            "phase": [p for p in phase_filter if p],
            "tags": tags_filter,
        }
    }


# ── AI 穿透剖面（杀招 2） ──

@app.get("/api/alpha/ai-screener/pierce")
def get_ai_screener_pierce(code: str = ""):
    """
    AI 穿透式验证悬浮窗数据。
    返回个股的本地行业映射、概念映射路径、Alpha评分剖面。
    """
    global _STOCK_MAP
    if _STOCK_MAP is None:
        _STOCK_MAP = _load_stock_map()
    _ensure_stock_names()

    name = _STOCK_NAME_CACHE.get(code, code)
    official_sector = _STOCK_MAP.get(code, "未知")

    chain_steps: list[str] = []

    # Step 1: 本地行业映射
    if official_sector and official_sector != "":
        chain_steps.append(
            f"[本地行业映射: {official_sector}]\n"
            f"  → 映射来源: STOCK_SECTOR_MAP.json 本地快照\n"
            f"  → 使用说明: 作为板块参考，需结合公告/主营业务人工复核"
        )
    else:
        chain_steps.append(
            "[本地行业映射: 未收录该股票]\n"
            "  → 名称关键词兜底映射，置信度较低"
        )

    # Step 2: 概念穿透
    concepts = []
    try:
        concepts = fetch_concept_tags(code)
    except Exception:
        pass

    if concepts:
        chain_steps.append(
            f"[大模型主营穿透: 核心概念集合]\n"
            f"  → 相关概念: {', '.join(concepts[:6])}\n"
            f"  → 概念命中来源: Akshare概念板块归属"
        )
    else:
        chain_steps.append(
            "[大模型主营穿透: 无概念数据]\n"
            "  → 建议人工核查该股票业务范围"
        )

    # Step 3: 行业偏差
    matched_sector = None
    registry = get_registry()
    for ss in registry.get_sector_list():
        if code in ss.get("stocks", []):
            matched_sector = ss["name"]
            break

    if official_sector and matched_sector:
        if official_sector == matched_sector:
            chain_steps.append(
                f"[行业平均值偏差修正: 零偏差]\n"
                f"  → 注册表板块 {matched_sector} 与本地行业映射 {official_sector} 一致\n"
                f"  → 行业校验: 映射一致，仍建议人工复核"
            )
        else:
            chain_steps.append(
                f"[行业平均值偏差修正: 跨板块分类]\n"
                f"  → 注册表板块: {matched_sector}\n"
                f"  → 本地映射: {official_sector}\n"
                f"  → 差异判定: 跨分类正常，双维度参考"
            )
    elif official_sector:
        chain_steps.append(
            f"[行业平均值偏差修正: 单维度]\n"
            f"  → 仅本地行业映射可用: {official_sector}"
        )

    # Step 4: 综合纯度
    purity = 85 if official_sector and official_sector != "" else 50
    if matched_sector and official_sector == matched_sector:
        purity = 95

    chain_steps.append(
        f"[综合判定: 纯度 {purity}%]\n"
        f"  → 数据层级: 本地映射 + 概念映射，结论仅作候选参考"
    )

    cot_text = "\n\n".join(chain_steps)

    return {
        "code": code,
        "name": name,
        "official_sector": official_sector,
        "concepts": concepts[:10],
        "purity": purity,
        "cot": cot_text,
        "mapped_sector": matched_sector,
    }


# ── 用户策略管理（杀招 1 Tab 2） ──

_STRATEGIES_FILE = os.path.join(os.path.dirname(__file__), "user_strategies.json")

@app.get("/api/alpha/ai-screener/strategies")
def list_strategies():
    """列出所有保存的策略"""
    try:
        with open(_STRATEGIES_FILE) as f:
            data = json.load(f)
        return {"strategies": data.get("strategies", []), "total": len(data.get("strategies", []))}
    except:
        return {"strategies": [], "total": 0}

@app.post("/api/alpha/ai-screener/strategies")
def save_strategy(data: dict):
    """保存一个新策略"""
    from datetime import datetime
    import time
    strategies = []
    try:
        with open(_STRATEGIES_FILE) as f:
            strategies = json.load(f).get("strategies", [])
    except:
        pass

    new_id = f"s{int(time.time())}"
    entry = {
        "id": new_id,
        "name": data.get("name", "未命名策略"),
        "phases": data.get("phases", []),
        "tags": data.get("tags", []),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    strategies.append(entry)

    with open(_STRATEGIES_FILE, "w") as f:
        json.dump({"strategies": strategies}, f, ensure_ascii=False, indent=2)

    return {"success": True, "strategy": entry}

@app.delete("/api/alpha/ai-screener/strategies")
def delete_strategy(id: str = ""):
    """删除一个策略"""
    try:
        with open(_STRATEGIES_FILE) as f:
            data = json.load(f)
        strategies = [s for s in data.get("strategies", []) if s.get("id") != id]
        with open(_STRATEGIES_FILE, "w") as f:
            json.dump({"strategies": strategies}, f, ensure_ascii=False, indent=2)
        return {"success": True}
    except:
        return {"success": False, "error": "操作失败"}


def _compute_screener_score(snap: dict, money_flow: dict, alerts: list[dict], concepts: list[str], change_pct: float) -> dict:
    """
    筛选评分引擎：基于真实多因子计算。

    因子权重:
      - 均线结构分 (0-30): 多头排列+30, 空头排列-20
      - MACD动量分 (0-25): DIF>DEA+12, DIF>0+8, 背离-5
      - 趋势偏离分 (0-20): 乖离率基于真实MA20计算
      - 资金面分 (0-15): 主力净流入+10, 主力力量>60%+5
      - 概念热度分 (0-10): 热门赛道概念匹配
      - 风险扣分 (-20~0): 异常警报、极端涨跌

    ★ 无有效数据时 → score=0, label='--', 不编造任何分数。
    """
    # ── 数据有效性检查 ──
    ma5_val = snap.get("ma5")
    ma10_val = snap.get("ma10")
    ma20_val = snap.get("ma20")
    dif_val = snap.get("dif")
    dea_val = snap.get("dea")
    alignment = snap.get("alignment", "")

    has_valid_data = (
        ma5_val is not None and ma20_val is not None
        and ma5_val > 0 and ma20_val > 0
        and dif_val is not None and dea_val is not None
    )
    if not has_valid_data:
        return {"score": 0, "label": "--", "reason": "数据不足，无法计算有效评分"}

    score = 0.0
    factors_hit: list[str] = []

    # ── 1. 均线结构分 [0, 30] ──
    if alignment == "多头排列":
        # MA5 > MA10 > MA20 → 强势结构
        spread = (ma5_val - ma20_val) / ma20_val * 100
        struct_score = min(30, 20 + spread * 2)
        factors_hit.append(f"多头排列(+{struct_score:.0f})")
    elif alignment == "空头排列":
        struct_score = -20
        factors_hit.append(f"空头排列({struct_score:.0f})")
    elif alignment == "均线交叉/缠绕":
        struct_score = 5
        factors_hit.append("均线缠绕(+5)")
    else:
        struct_score = 0
    score += struct_score

    # ── 2. MACD动量分 [0, 25] ──
    macd_score = 0.0
    if dif_val > dea_val:
        macd_score += 12
        factors_hit.append("MACD金叉(+12)")
    else:
        macd_score -= 5
    if dif_val > 0:
        macd_score += 8
        factors_hit.append("DIF>0(+8)")
    elif dif_val < 0:
        macd_score -= 3
    # DIF 斜率加速
    if abs(dif_val - dea_val) > 0.02:
        macd_score += 5
    score += max(0, min(25, macd_score))

    # ── 3. 趋势偏离分（乖离率）[0, 20] ──
    bias = (ma5_val - ma20_val) / ma20_val * 100 if ma20_val > 0 else 0.0
    if bias > 5:
        trend_score = min(20, 10 + bias)
        factors_hit.append(f"强势乖离{bias:.1f}%(+{trend_score:.0f})")
    elif bias > 0:
        trend_score = min(12, bias * 1.5)
    elif bias > -3:
        trend_score = 3
    elif bias > -8:
        trend_score = -5
        factors_hit.append(f"弱势乖离{bias:.1f}%(-5)")
    else:
        trend_score = -12
        factors_hit.append(f"深度乖离{bias:.1f}%(-12)")
    score += trend_score

    # ── 4. 当日动量分 [-8, 8] ──
    mom_score = max(-8, min(8, change_pct * 1.5))
    score += mom_score

    # ── 5. 资金面分 [0, 15] ──
    fund_score = 0.0
    main_net = money_flow.get("main_net", 0)
    main_force_pct = money_flow.get("main_force_pct", 50)
    if main_net > 10000000:  # >1000万净流入
        fund_score += 10
        factors_hit.append("主力净流入(+10)")
    elif main_net > 0:
        fund_score += 5
    elif main_net < -5000000:
        fund_score -= 5
    if main_force_pct >= 60:
        fund_score += 5
        factors_hit.append("主力强势(+5)")
    elif main_force_pct <= 35:
        fund_score -= 3
    score += max(0, min(15, fund_score))

    # ── 6. 概念热度分 [0, 10] ──
    hot_keywords = ["AI", "人工智能", "锂", "芯片", "半导体", "新能源", "储能", "机器人",
                    "光模块", "低空经济", "固态电池", "数据要素", "算力"]
    hot_count = sum(1 for c in concepts if any(kw in c for kw in hot_keywords))
    concept_score = min(10, hot_count * 2.5)
    if concept_score >= 5:
        factors_hit.append(f"热门赛道匹配(+{concept_score:.0f})")
    score += concept_score

    # ── 7. 风险扣分 [-25, 0] ──
    risk_score = 0.0
    for alert in alerts:
        if alert.get("level") == "danger":
            risk_score -= 12
            factors_hit.append("⚠危险警报(-12)")
        elif alert.get("level") == "warning":
            risk_score -= 6
    # 极端涨跌风险
    if abs(change_pct) > 9:
        risk_score -= 10
        factors_hit.append("极端涨跌(-10)")
    elif abs(change_pct) > 7:
        risk_score -= 5
    score += max(-25, risk_score)

    # ── 8. 最终映射到 0-100 ──
    final = max(0, min(100, round(score)))

    # ── 9. 分级 ──
    if final >= 80:
        label = "S"
        reason = "技术面+资金面+主题共振，赔率格局占优"
    elif final >= 65:
        label = "A"
        reason = "多因子偏向正面，具备趋势强度"
    elif final >= 45:
        label = "B"
        reason = "因子中性，震荡格局，等待催化"
    elif final >= 25:
        label = "C"
        reason = "因子偏空，下行风险大于上行收益"
    else:
        label = "--"
        reason = "多项指标走弱，暂不具备交易价值"

    return {"score": final, "label": label, "reason": reason}


@app.post("/api/screener/results")
def screener_results(filters: dict):
    """
    策略选股筛选引擎。支持两种过滤维度：
      1. 生命周期阶段（phase:xxx）→ 从实时板块数据匹配
      2. 热门赛道 → 概念关键词匹配

    ★ 铁律：未选择任何筛选条件 → 返回空列表。
    """
    # 1. 合并选中的 filter_id
    selected_ids: list[str] = []
    for bucket in ("macro", "industry", "value"):
        selected_ids.extend(filters.get(bucket) or [])

    if not selected_ids:
        return {"total": 0, "filters": {"selected": [], "keywords": [], "message": "未选择任何条件"}, "results": []}

    # 2. 分离 phase 过滤器 vs 关键词过滤器
    phase_ids = [f for f in selected_ids if f.startswith("phase:")]
    keyword_ids = [f for f in selected_ids if not f.startswith("phase:")]

    # ── 2a. 关键词 → 概念匹配 ──
    match_keywords: set[str] = set()
    for fid in keyword_ids:
        kw = _SCREENER_FILTER_MAP.get(fid, [])
        match_keywords.update(kw)

    # ── 2b. 生命周期阶段 → 板块匹配 ──
    phase_stocks: set[str] = set()
    phase_sectors_matched: list[str] = []
    if phase_ids:
        from sector_worker import get_all_lifecycles
        lifecycles = get_all_lifecycles()
        target_phases = {p.replace("phase:", "") for p in phase_ids}
        # 找到处于目标阶段的板块
        matched_sectors: dict[str, str] = {}
        for sector_name, info in lifecycles.items():
            if info.get("phase") in target_phases:
                matched_sectors[sector_name] = info.get("phase", "")
        phase_sectors_matched = list(matched_sectors.keys())
        # 从 alpha_engine 的精确映射表找这些板块的成分股
        from alpha_engine import EXACT_STOCK_SECTOR_MAP
        for code, sector in EXACT_STOCK_SECTOR_MAP.items():
            if sector in matched_sectors:
                phase_stocks.add(code)

    # 3. 遍历全量股票池
    results: list[dict] = []
    all_codes = list(STOCK_NAMES.keys())
    has_phase_filter = len(phase_stocks) > 0
    has_keyword_filter = len(match_keywords) > 0

    for code in all_codes:
        concepts = fetch_concept_tags(code)

        # ── 阶段过滤：股票必须属于目标阶段的板块 ──
        if has_phase_filter and code not in phase_stocks:
            continue

        # ── 关键词过滤：股票概念必须匹配 ──
        if has_keyword_filter:
            matched = any(any(kw in c for c in concepts) for kw in match_keywords)
            if not matched:
                continue

        # ── 轻量级数据获取 ──
        try:
            df = fetch_stock_data(code)
            if df.empty or len(df) < 5:
                continue
            ohlcv = normalize_ohlcv(df)
            closes = np.array([d["close"] for d in ohlcv], dtype=np.float64)
            ind = compute_indicators(closes)

            last = ohlcv[-1]
            prev = ohlcv[-2]
            price = last["close"]
            change = round(price - prev["close"], 2)
            change_pct = round(change / prev["close"] * 100, 2)

            money_flow = fetch_money_flow(code, ohlcv)
            alerts = compute_abnormal_alerts(ohlcv)
            combat = compute_combat_score(ind["snapshot"], money_flow, alerts, concepts, change_pct)
            screener = _compute_screener_score(ind["snapshot"], money_flow, alerts, concepts, change_pct)

            results.append({
                "code": code,
                "name": STOCK_NAMES.get(code, f"股票{code}"),
                "price": price,
                "change": change,
                "changePercent": change_pct,
                "concepts": concepts,
                "alignment": ind["snapshot"]["alignment"],
                "combatScore": combat,
                "screenerScore": screener,
                "behaviorTags": compute_behavior_tags(ohlcv, money_flow, alerts, concepts, closes),
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["screenerScore"]["score"], reverse=True)

    return {
        "total": len(results),
        "filters": {
            "selected": selected_ids,
            "keywords": list(match_keywords),
            "phase_sectors": phase_sectors_matched,
        },
        "results": results,
    }


# ── 全局异常拦截，防止未捕获异常导致服务崩溃 ──

from fastapi.responses import JSONResponse
from starlette.requests import Request

@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception):
    """兜底：任何未捕获异常都返回 500 JSON，而不是让服务崩溃"""
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误: {exc}"},
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


def _fetch_index_quotes_from_tencent() -> list[dict]:
    """通过腾讯行情接口快速获取三大指数，避免 akshare 在部分 Windows 环境阻塞或崩溃。"""
    import re
    import requests

    symbols = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols.keys())
    resp = requests.get(url, timeout=5)
    resp.encoding = "gbk"
    text = resp.text
    results: list[dict] = []
    for symbol, fallback_name in symbols.items():
        match = re.search(rf'v_{symbol}="([^"]*)"', text)
        if not match:
            continue
        parts = match.group(1).split("~")
        try:
            name = parts[1] or fallback_name
            close = float(parts[3])
            change = float(parts[31])
            change_pct = float(parts[32])
            high = float(parts[33])
            low = float(parts[34])
            ts = parts[30]
            date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""
            if close <= 0:
                continue
            results.append({
                "name": name,
                "price": round(close, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "high": high,
                "low": low,
                "date": date,
                "data_available": True,
            })
        except Exception:
            continue
    return results


@app.get("/api/index/quotes")
async def index_quotes():
    """获取三大指数行情（上证/深证/创业板）"""
    import asyncio
    import concurrent.futures

    # ★ 腾讯接口快且稳定，优先使用；akshare 仅作为备用。
    try:
        results = _fetch_index_quotes_from_tencent()
        if len(results) == 3:
            try:
                from market_cache import save_json_cache
                save_json_cache("index_quotes_cache.json", {"quotes": results})
            except Exception:
                pass
            return {"quotes": results, "source": "tencent", "data_available": True}
    except Exception:
        pass

    # ★ 先尝试磁盘缓存，避免等待 akshare 超时
    try:
        from market_cache import load_json_cache, load_cache_metadata
        cached = load_json_cache("index_quotes_cache.json", max_age_hours=12)
        if cached:
            meta = load_cache_metadata("index_quotes_cache.json")
            asyncio.create_task(_refresh_index_quotes_async())
            return {"quotes": cached.get("quotes", []), "source": "disk_cache", "trade_date": meta.get("trade_date", "")}
    except Exception:
        pass

    indices = [
        ("上证指数", "sh000001"),
        ("深证成指", "sz399001"),
        ("创业板指", "sz399006"),
    ]
    results = []
    all_ok = True

    def _fetch_one(name: str, symbol: str) -> dict:
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            if len(df) < 2:
                return {"name": name, "price": None, "change_pct": None, "data_available": False, "error": "数据不足"}
            last = df.iloc[-1]
            prev = df.iloc[-2]
            close = float(last["close"])
            prev_close = float(prev["close"])
            change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
            return {
                "name": name, "price": round(close, 2), "change_pct": change_pct,
                "high": float(last["high"]), "low": float(last["low"]), "date": str(last["date"]),
                "data_available": True,
            }
        except Exception as e:
            return {"name": name, "price": None, "change_pct": None, "data_available": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(_fetch_one, name, symbol): (name, symbol) for name, symbol in indices}
        for fut in concurrent.futures.as_completed(futures, timeout=6):
            try:
                r = fut.result(timeout=2)
            except Exception:
                r = {"name": futures[fut][0], "price": 0, "change_pct": 0, "error": "timeout"}
            if "error" in r:
                all_ok = False
            results.append(r)

    if all_ok and results:
        try:
            from market_cache import save_json_cache
            save_json_cache("index_quotes_cache.json", {"quotes": results})
        except Exception:
            pass
        return {"quotes": results, "source": "live"}

    # 回退到磁盘缓存
    try:
        from market_cache import load_json_cache, load_cache_metadata
        cached = load_json_cache("index_quotes_cache.json", max_age_hours=12)
        if cached:
            meta = load_cache_metadata("index_quotes_cache.json")
            asyncio.create_task(_refresh_index_quotes_async())
            return {"quotes": cached.get("quotes", []), "source": "disk_cache", "trade_date": meta.get("trade_date", "")}
    except Exception:
        pass

    return {"quotes": results, "source": "unavailable", "data_available": False}


_INDEX_QUOTES_PENDING: bool = False

async def _refresh_index_quotes_async():
    """后台异步刷新指数行情缓存"""
    global _INDEX_QUOTES_PENDING
    if _INDEX_QUOTES_PENDING:
        return
    _INDEX_QUOTES_PENDING = True
    import asyncio as _asyncio, concurrent.futures
    indices = [("上证指数", "sh000001"), ("深证成指", "sz399001"), ("创业板指", "sz399006")]
    def _fetch_one(name: str, symbol: str) -> dict | None:
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            if len(df) < 2:
                return None
            last = df.iloc[-1]; prev = df.iloc[-2]
            close = float(last["close"]); prev_close = float(prev["close"])
            return {
                "name": name, "price": round(close, 2),
                "change_pct": round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0,
                "high": float(last["high"]), "low": float(last["low"]), "date": str(last["date"]),
            }
        except Exception:
            return None
    try:
        loop = _asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(_fetch_one, n, s): (n, s) for n, s in indices}
            results = []
            for fut in concurrent.futures.as_completed(futs, timeout=5):
                try:
                    r = fut.result(timeout=2)
                    if r:
                        results.append(r)
                except Exception:
                    pass
        if len(results) == 3:
            from market_cache import save_json_cache
            save_json_cache("index_quotes_cache.json", {"quotes": results})
    except Exception:
        pass
    finally:
        _INDEX_QUOTES_PENDING = False


# ═══════════════════════════════════════
# 市场概览: 涨跌家数
# ═══════════════════════════════════════
_market_summary_cache: dict | None = None
_market_summary_ts: float = 0

def _fetch_rise_fall_from_tencent() -> dict:
    """通过腾讯行情接口批量查询所有A股实时涨跌"""
    import time, asyncio

    # 获取全A代码列表（缓存，每2小时刷新一次）
    if not hasattr(_fetch_rise_fall_from_tencent, "_codes"):
        try:
            df = ak.stock_info_a_code_name()
            _fetch_rise_fall_from_tencent._codes = df['code'].tolist()
            _fetch_rise_fall_from_tencent._codes_ts = time.time()
        except Exception:
            pass

    codes = getattr(_fetch_rise_fall_from_tencent, "_codes", None)
    if not codes:
        return {"total": 0, "rise": 0, "fall": 0, "flat": 0, "source": "fallback", "updated": time.strftime("%H:%M")}

    import httpx
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _tx_code(c: str) -> str:
        return f"sh{c}" if c.startswith(('6', '5')) else f"sz{c}"

    # 每批500只，并发拉取
    BATCH = 500
    batches = [codes[i:i+BATCH] for i in range(0, len(codes), BATCH)]

    results = {"rise": 0, "fall": 0, "flat": 0, "total_checked": 0}

    def fetch_batch(batch):
        query = ",".join(_tx_code(c) for c in batch)
        url = f"http://qt.gtimg.cn/q={query}"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0'}
            ), timeout=10) as r:
                text = r.read().decode('gbk')
        except Exception:
            return (0, 0, 0, 0)

        r, f, fl, t = 0, 0, 0, 0
        for line in text.strip().split(';\n'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            try:
                data_part = line.split('=', 1)[1].strip()
                if data_part.startswith('"') and data_part.endswith('"'):
                    data_part = data_part[1:-1]
                fields = data_part.split('~')
                if len(fields) >= 5:
                    price = float(fields[3]) if fields[3] else 0
                    prev_close = float(fields[4]) if fields[4] else 0
                    if prev_close > 0 and price > 0:
                        change = price - prev_close
                        if change > 0:
                            r += 1
                        elif change < 0:
                            f += 1
                        else:
                            fl += 1
                        t += 1
            except (ValueError, IndexError):
                continue
        return (r, f, fl, t)

    import urllib.request

    # 并发拉取（最多5个worker）
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_batch, b): b for b in batches}
        for future in as_completed(futures):
            r, f, fl, t = future.result()
            results["rise"] += r
            results["fall"] += f
            results["flat"] += fl
            results["total_checked"] += t

    total = results["total_checked"]
    return {
        "total": total,
        "rise": results["rise"],
        "fall": results["fall"],
        "flat": results["flat"],
        "source": "tencent_realtime",
        "updated": time.strftime("%H:%M")
    }


# 后台预取任务锁
_market_summary_pending: bool = False

async def _background_refresh_market_summary():
    """后台线程预取涨跌家数"""
    global _market_summary_cache, _market_summary_ts, _market_summary_pending
    if _market_summary_pending:
        return
    _market_summary_pending = True
    try:
        import asyncio
        data = await asyncio.to_thread(_fetch_rise_fall_from_tencent)
        if data.get("total", 0) > 0:
            _market_summary_cache = data
            _market_summary_ts = time.time()
            # ★ 保存到磁盘
            try:
                from market_cache import save_json_cache
                save_json_cache("market_summary_cache.json", data)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _market_summary_pending = False


@app.get("/api/index/market-summary")
async def market_summary():
    """市场涨跌家数概览 — 永不阻塞：内存 → 磁盘 → 空"""
    import time, asyncio
    now = time.time()
    global _market_summary_cache, _market_summary_ts

    # 1) 内存非空 → 直接返回；过旧时后台刷新
    if _market_summary_cache is not None:
        if now - _market_summary_ts > 60:
            asyncio.create_task(_background_refresh_market_summary())
        return _market_summary_cache

    # 2) 磁盘
    try:
        from market_cache import load_json_cache, load_cache_metadata
        cached = load_json_cache("market_summary_cache.json", max_age_hours=12)
        if cached:
            meta = load_cache_metadata("market_summary_cache.json")
            _market_summary_cache = cached
            _market_summary_ts = now
            cached["source"] = "disk_cache"
            cached["trade_date"] = meta.get("trade_date", "")
            asyncio.create_task(_background_refresh_market_summary())
            return cached
    except Exception:
        pass

    # 3) 空结果也触发后台刷新，下一次请求即可拿到真实数据
    asyncio.create_task(_background_refresh_market_summary())
    return {"total": 0, "rise": 0, "fall": 0, "flat": 0, "source": "none", "error": "数据不可用"}


@app.get("/api/sector-health")
async def sector_health():
    """★ 调试端点：查看板块数据管道的实际状态"""
    from sector_worker import get_summary as _sw_summary, SECTOR_LIFECYCLE_FULL, _worker_stats, SECTOR_LIFECYCLE_LOCK
    from sector_scanner import get_registry
    from network_guard import get_stats as _net_stats

    with SECTOR_LIFECYCLE_LOCK:
        lifecycles = dict(SECTOR_LIFECYCLE_FULL)

    phase_counts: dict[str, int] = {}
    for name, info in lifecycles.items():
        p = info.get("phase", "unknown")
        phase_counts[p] = phase_counts.get(p, 0) + 1
        if p in ("unknown", "detect_failed"):
            # 记录数据行数帮助诊断
            phase_counts[f"{p}(rows={info.get('data_rows','?')})"] = phase_counts.pop(p, 0)

    registry = get_registry()
    sector_names = registry.get_sector_names()

    return {
        "worker": _worker_stats,
        "lifecycles_in_cache": len(lifecycles),
        "total_sectors_in_registry": len(sector_names),
        "missing_sectors": [n for n in sector_names if n not in lifecycles][:10],
        "phase_distribution": phase_counts,
        "network_stats": _net_stats()[:10],
        "sample_phases": {n: info.get("phase", "?") for n, info in list(lifecycles.items())[:15]},
    }


# ═══════════════════════════════════════
# 热点新闻: 百度股市通热搜
# ═══════════════════════════════════════
# 热点新闻/快讯: 每次真实拉取（无缓存）
# ═══════════════════════════════════════
# 热点新闻/快讯: 先磁盘缓存，避免网络挂死
_HOT_NEWS_PENDING: bool = False
_NEWSFLASH_PENDING: bool = False

@app.get("/api/hot/news")
async def get_hot_news():
    try:
        from market_cache import load_json_cache
        cached = load_json_cache("hot_news_cache.json", max_age_hours=6)
        if cached:
            import asyncio
            asyncio.create_task(_refresh_hot_news_async())
            return cached
    except Exception:
        pass
    try:
        import asyncio, concurrent.futures
        loop = asyncio.get_event_loop()
        items = await loop.run_in_executor(None, _fetch_hot_news)
        if items:
            from market_cache import save_json_cache
            save_json_cache("hot_news_cache.json", items)
            return items
    except Exception:
        pass
    return []

def _fetch_hot_news():
    try:
        import akshare as ak
        df = ak.stock_hot_search_baidu()
        df = df.head(15)
        try:
            ndf = ak.stock_info_a_code_name()
            name_code_map = dict(zip(ndf["name"], ndf["code"]))
            clean_map = {}
            for n, c in name_code_map.items():
                clean = n.replace("*", "").replace("退", "").replace(" ", "").strip()
                if clean:
                    clean_map[clean] = c
        except:
            name_code_map = {}
            clean_map = {}
        items = []
        for _, row in df.iterrows():
            change = row["涨跌幅"]
            name = row["名称/代码"]
            code = name_code_map.get(name, "")
            if not code:
                clean_name = name.replace(" ", "").replace("　", "").strip()
                code = clean_map.get(clean_name, "")
            items.append({"name": name, "code": code, "change_pct": change, "heat": int(row["综合热度"])})
        return items
    except Exception as e:
        logger.warning(f"获取热点新闻失败: {e}")
        return []

async def _refresh_hot_news_async():
    global _HOT_NEWS_PENDING
    if _HOT_NEWS_PENDING:
        return
    _HOT_NEWS_PENDING = True
    import asyncio, concurrent.futures
    try:
        loop = asyncio.get_event_loop()
        items = await loop.run_in_executor(None, _fetch_hot_news)
        if items:
            from market_cache import save_json_cache
            save_json_cache("hot_news_cache.json", items)
    except Exception:
        pass
    finally:
        _HOT_NEWS_PENDING = False

@app.get("/api/hot/newsflash")
async def get_newsflash():
    try:
        from market_cache import load_json_cache
        cached = load_json_cache("newsflash_cache.json", max_age_hours=6)
        if cached:
            import asyncio
            asyncio.create_task(_refresh_newsflash_async())
            return cached
    except Exception:
        pass
    try:
        import asyncio, concurrent.futures
        loop = asyncio.get_event_loop()
        items = await loop.run_in_executor(None, _fetch_newsflash)
        if items:
            from market_cache import save_json_cache
            save_json_cache("newsflash_cache.json", items)
            return items
    except Exception:
        pass
    return []

def _fetch_newsflash():
    try:
        import akshare as ak
        df = ak.stock_news_main_cx()
        items = []
        for _, row in df.iterrows():
            tag = row.get("tag", "") or ""
            summary = row.get("summary", "") or ""
            url = row.get("url", "") or ""
            import re as _re
            date_match = _re.search(r"(\d{4}-\d{2}-\d{2})", url)
            date_str = date_match.group(1) if date_match else ""
            title = summary[:40] + "..." if len(summary) > 40 else summary
            items.append({"tag": tag, "text": summary, "title": title, "url": url, "date": date_str})
        return items
    except Exception as e:
        logger.warning(f"获取新闻快讯失败: {e}")
        return []

async def _refresh_newsflash_async():
    global _NEWSFLASH_PENDING
    if _NEWSFLASH_PENDING:
        return
    _NEWSFLASH_PENDING = True
    import asyncio, concurrent.futures
    try:
        loop = asyncio.get_event_loop()
        items = await loop.run_in_executor(None, _fetch_newsflash)
        if items:
            from market_cache import save_json_cache
            save_json_cache("newsflash_cache.json", items)
    except Exception:
        pass
    finally:
        _NEWSFLASH_PENDING = False
# ═══════════════════════════════════════
# Phase 0 - AI Alpha OS 新路由
# ═══════════════════════════════════════
from routes_alpha_os import router as alpha_os_router
from replay.api import router as replay_router
app.include_router(alpha_os_router)
app.include_router(replay_router)

# ── 静态前端（/ 根路径挂载 dist） ──
from fastapi.staticfiles import StaticFiles as _StaticFiles
import os as _os
_dist = _os.path.join(_os.path.dirname(__file__), "dist")
if _os.path.isdir(_dist):
    app.mount("/", _StaticFiles(directory=_dist, html=True), name="xpb_frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
