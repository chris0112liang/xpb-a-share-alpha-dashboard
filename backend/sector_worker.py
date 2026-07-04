"""
sector_worker.py  —  异步板块生命周期计算后台（TX API 版）

设计：
  1. 单一线程 + 队列机制，不阻塞主线程
  2. 从 SECTOR_REGISTRY 取出成分股代码
  3. 批量获取个股 TX 行情 → 按板块聚合计算生命周期
  4. 每 30 分钟完成一轮全市场扫描
  5. 结果写入全量板块生命周期缓存

性能保证：
  - 批量获取个股行情（每次最多 5 只）
  - numpy 向量化指标计算
  - 个股数据按板块聚合 → 以板块加权均价/成交量代替板块指数
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import numpy as np
import pandas as pd

from sector_scanner import get_registry

# ── 配置 ──

POLL_INTERVAL_SECONDS = 30 * 60        # 30 分钟全市场一轮
BATCH_SIZE = 5                         # 每批并发获取个股数

# ── 全局缓存 ──

SECTOR_LIFECYCLE_FULL: dict[str, dict] = {}
SECTOR_LIFECYCLE_LOCK = threading.Lock()
_worker_running = False
_worker_thread: Optional[threading.Thread] = None
_worker_stats: dict = {
    "rounds": 0,
    "last_update": None,
    "sectors_updated": 0,
    "avg_time_sec": 0,
}


# ── 个股 TX 数据获取 ──

def _fetch_stock_tx(code: str, days: int = 100) -> Optional[pd.DataFrame]:
    """
    获取个股日行情。★ 绕过全局熔断器，双源 fallback（腾讯 → EastMoney）。

    返回 None 仅当两个源都失败。
    """
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = f"{prefix}{code}"
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    required_cols = {"open", "close", "high", "low"}

    # ── 方案 A: TX API（直接调用，不走 safe_call 避免熔断） ──
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(
                ak.stock_zh_a_hist_tx,
                symbol=symbol, start_date=start, end_date=end, adjust="qfq",
            )
            df = _fut.result(timeout=8.0)
        if df is not None and not df.empty and len(df) >= 5:
            cols = set(df.columns)
            if "close" in cols and "open" in cols:
                if "date" not in cols and "日期" in cols:
                    df = df.rename(columns={"日期": "date"})
                if "date" in df.columns:
                    return df
    except Exception:
        pass

    # ── 方案 B: EastMoney fallback ──
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(
                ak.stock_zh_a_hist,
                symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq",
            )
            df = _fut.result(timeout=8.0)
        if df is not None and not df.empty and len(df) >= 5:
            em_rename = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            }
            existing_renames = {k: v for k, v in em_rename.items() if k in df.columns}
            if existing_renames:
                df = df.rename(columns=existing_renames)
            cols = set(df.columns)
            if "close" in cols and len(df) >= 5:
                if "date" not in cols and "日期" in df.columns:
                    df = df.rename(columns={"日期": "date"})
                if "date" in df.columns:
                    return df
    except Exception:
        pass

    return None




from sector_lifecycle_6stage import compute_6stage_lifecycle, PHASE_CN as LIFECYCLE_PHASE_CN


def _aggregate_sector_from_stocks(
    sector_name: str, stock_codes: list[str]
) -> Optional[dict]:
    """
    通过个股行情聚合计算板块生命周期指标。

    ★ 铁血健壮性：
      1. 最多尝试 3 只成分股 → 任一只成功即用
      2. 聚合后 forward-fill + backward-fill 清洗缺失值
      3. 详细日志记录失败原因
    """
    dfs: dict[str, pd.DataFrame] = {}
    errors: list[str] = []

    # ★ 尝试最多 2 只股票，任一只成功即可（每只 8s TX → 8s EM）
    for code in stock_codes[:2]:
        df = _fetch_stock_tx(code)
        if df is not None and len(df) >= 5:
            df = df.set_index("date") if "date" in df.columns else df
            dfs[code] = df
            break
        else:
            errors.append(f"{code}:无数据")

    if not dfs:
        print(f"[SectorWorker] ⚠ {sector_name} 全部成分股获取失败: {'; '.join(errors[:2])}", flush=True)
        # ★ 不返回 None — 返回一个占位状态，让前端至少显示"感知中断"而非完全消失
        return {
            "phase": "detect_failed",
            "confidence": 0.0,
            "phase_seq": -1,
            "scores": {},
            "bias": 0.0,
            "price_mom_5": 0.0, "price_mom_20": 0.0,
            "vol_mom_5": 0.0, "vol_ratio": 1.0,
            "acceleration": 0.0, "rs_slope": 0.0, "vol_trend": 0.0,
            "days_active": 0, "consistency": 0.0,
            "strength_score": 0.0, "is_turning": False,
            "vol_mom": 0.0, "total_score": 0.0, "price_mom_10": 0.0,
            "data_rows": 0,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 按日期合并所有成分股的 close, amount ──
    all_closes: list[pd.Series] = []
    all_amounts: list[pd.Series] = []

    for code, df in dfs.items():
        c = df["close"].astype(float)
        a = df["amount"].astype(float) if "amount" in df.columns else None
        all_closes.append(c)
        if a is not None:
            all_amounts.append(a)

    if not all_closes:
        return None

    # 合并：等权平均计算板块价格、成交量
    close_df = pd.concat(all_closes, axis=1)
    amount_df = pd.concat(all_amounts, axis=1) if all_amounts else None

    close_avg = close_df.mean(axis=1).sort_index()
    amount_avg = amount_df.mean(axis=1).sort_index() if amount_df is not None else close_avg * 0 + 1

    # ═══════════════════════════════════════
    # ★ 铁血清洗：向前填充缺失值，不因少数缺失砍掉整个板块
    # ═══════════════════════════════════════
    close_avg = close_avg.ffill().bfill()
    amount_avg = amount_avg.ffill().bfill()
    close_avg = close_avg.dropna()
    amount_avg = amount_avg.dropna()

    close_vals = close_avg.values
    amount_vals = amount_avg.values
    n = len(close_vals)

    if n < 10:
        print(f"[SectorWorker] {sector_name} 有效数据不足: {n}行 (ffill后)", flush=True)
        return None

    # ═══════════════════════════════════
    # 6 阶段生命周期引擎
    # ═══════════════════════════════════
    import numpy as _np
    result = compute_6stage_lifecycle(
        _np.array(close_vals[-100:]),
        _np.array(amount_vals[-100:]),
    )

    # 原字段兼容
    result["vol_mom"] = result.get("vol_trend", 0)
    result["total_score"] = result.get("strength_score", 0)
    result["price_mom_10"] = result.get("price_mom_20", 0)
    result["data_rows"] = n

    return result


# ── 批量扫描 ──

def _run_scan_round() -> int:
    """执行一轮扫描，3 并发处理板块，整轮最多 300 秒"""
    registry = get_registry()

    active_sectors = registry.get_sector_list()
    if not active_sectors:
        print("[SectorWorker] 注册表为空，跳过本轮", flush=True)
        return 0

    results: dict[str, dict] = {}
    total = len(active_sectors)
    batch_start = time.time()
    TIMEOUT = 300
    CONCURRENCY = 3  # ★ 3 个板块并行抓取

    import concurrent.futures as _cf

    # 按 sector name 稳定性排序，优先处理有数据的行业板块
    priority_order = sorted(
        active_sectors,
        key=lambda s: (s.get("board_type") != "industry", s.get("name", "")),
    )

    with _cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        future_map: dict[_cf.Future, str] = {}
        sector_iter = iter(priority_order)
        pending = 0

        # 提交初始 CONCURRENCY 个任务
        for _ in range(CONCURRENCY):
            try:
                s = next(sector_iter)
                name = s["name"]
                stocks = s.get("stocks", [])
                if stocks:
                    fut = pool.submit(_aggregate_sector_from_stocks, name, stocks[:2])
                    future_map[fut] = name
                    pending += 1
            except StopIteration:
                break

        # 处理完成的任务并提交新任务
        processed = 0
        while future_map:
            elapsed = time.time() - batch_start
            if elapsed > TIMEOUT and processed >= len(future_map):
                break

            done, _ = _cf.wait(
                future_map.keys(),
                timeout=2.0,
                return_when=_cf.FIRST_COMPLETED,
            )

            for fut in done:
                name = future_map.pop(fut)
                processed += 1
                try:
                    lifecycle = fut.result(timeout=0)
                    if lifecycle is not None:
                        results[name] = lifecycle
                except Exception:
                    pass

                # 提交下一个板块
                try:
                    s = next(sector_iter)
                    next_name = s["name"]
                    next_stocks = s.get("stocks", [])
                    if next_stocks:
                        next_fut = pool.submit(_aggregate_sector_from_stocks, next_name, next_stocks[:2])
                        future_map[next_fut] = next_name
                except StopIteration:
                    pass

            # 超时保护
            if time.time() - batch_start > TIMEOUT:
                remaining = len(future_map)
                print(f"[SectorWorker] 超时({TIMEOUT}s)，已处理 {processed}/{total}，剩余 {remaining} 个取消", flush=True)
                for f in list(future_map.keys()):
                    f.cancel()
                    future_map.pop(f, None)
                break

    elapsed = time.time() - batch_start
    print(
        f"[SectorWorker] 数据获取: {len(results)}/{total} 板块 ({elapsed:.1f}s | {CONCURRENCY}并发)",
        flush=True,
    )

    # 写入全局缓存
    with SECTOR_LIFECYCLE_LOCK:
        SECTOR_LIFECYCLE_FULL.clear()
        SECTOR_LIFECYCLE_FULL.update(results)

    # ★ 持久化到磁盘
    try:
        from market_cache import save_json_cache
        save_json_cache("sector_lifecycles_cache.json", results)
    except Exception as e:
        print(f"[SectorWorker] 磁盘缓存保存失败: {e}", flush=True)

    update_count = len(results)
    _worker_stats["rounds"] += 1
    _worker_stats["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _worker_stats["sectors_updated"] = update_count
    _worker_stats["avg_time_sec"] = round(
        (_worker_stats["avg_time_sec"] * (_worker_stats["rounds"] - 1) + elapsed)
        / _worker_stats["rounds"], 1
    )

    return update_count


# ── 后台线程 ──

def worker_loop():
    """后台线程主循环：先加载磁盘缓存，再等待15秒后开始扫描"""
    global _worker_running
    _worker_running = True
    print("[SectorWorker] 后台线程启动 (TX API)", flush=True)

    # ★ 启动时先从磁盘恢复缓存（不等首轮扫描）
    try:
        from market_cache import load_json_cache
        cached = load_json_cache("sector_lifecycles_cache.json", max_age_hours=48)
        if cached:
            with SECTOR_LIFECYCLE_LOCK:
                SECTOR_LIFECYCLE_FULL.clear()
                SECTOR_LIFECYCLE_FULL.update(cached)
            print(f"[SectorWorker] 从磁盘恢复 {len(cached)} 个板块缓存", flush=True)
    except Exception as e:
        print(f"[SectorWorker] 磁盘缓存加载失败: {e}", flush=True)

    # 等待 15 秒让应用完全初始化
    time.sleep(15)

    # 首次立即执行一轮
    try:
        count = _run_scan_round()
        print(f"[SectorWorker] 首轮更新: {count} 板块", flush=True)
    except Exception as e:
        print(f"[SectorWorker] 首轮失败: {e}", flush=True)

    while _worker_running:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
            count = _run_scan_round()
            print(f"[SectorWorker] 定时更新完成: {count} 板块", flush=True)
        except Exception as e:
            print(f"[SectorWorker] 更新异常: {e}", flush=True)

    print("[SectorWorker] 后台线程退出", flush=True)


def start_worker():
    """启动后台板块扫描线程（非阻塞）"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        print("[SectorWorker] 已在运行", flush=True)
        return
    _worker_thread = threading.Thread(target=worker_loop, daemon=True, name="sector-worker")
    _worker_thread.start()


def stop_worker():
    global _worker_running
    _worker_running = False


def get_summary() -> dict:
    """获取汇总信息"""
    with SECTOR_LIFECYCLE_LOCK:
        lifecycles = dict(SECTOR_LIFECYCLE_FULL)

    phase_counts: dict[str, int] = {}
    for info in lifecycles.values():
        p = info.get("phase", "noise")
        phase_counts[p] = phase_counts.get(p, 0) + 1

    strength_sectors = sorted(
        [
            (n, i) for n, i in lifecycles.items()
            if i.get("phase") in ("strengthening", "initiation", "startup", "main_rise_1", "acceleration")
        ],
        key=lambda x: -x[1].get("confidence", 0),
    )[:10]

    return {
        "total_sectors": len(lifecycles),
        "phase_distribution": phase_counts,
        "top_strength": [
            {"name": n, "phase": i.get("phase"), "confidence": i.get("confidence"), "bias": i.get("bias")}
            for n, i in strength_sectors
        ],
        "worker_stats": _worker_stats,
    }


def get_all_lifecycles() -> dict[str, dict]:
    with SECTOR_LIFECYCLE_LOCK:
        return dict(SECTOR_LIFECYCLE_FULL)
