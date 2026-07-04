"""
network_guard.py — 网络请求统一防护层

所有外部 API 调用（AKShare、Sina、EM）经过此层：
1. 统一超时（函数级 timeout）
2. 重试（fallback 链）
3. 优雅返回 None/空值（绝不阻塞主进程）
4. 速率限制（避免 OOM）

用法:
    from network_guard import safe_call, ak_wrapper
    
    result = safe_call(ak.stock_zh_a_spot_em, timeout=10)
    df = ak_wrapper("stock_zh_a_spot_em", timeout=10)
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 全局公用的单线程执行器（AKShare 内部会 spawn 线程，加控制避免 OOM）
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="safe-call")

# 调用统计
_call_stats: dict[str, dict] = {}
_stats_lock = threading.Lock()

# 熔断器：连续 5 次失败后，30 秒内直接返回 fallback 不发起网络请求
_fail_count: int = 0
_fail_unlock_at: float = 0.0
_circuit_lock = threading.Lock()


def safe_call(
    fn: Callable[..., T],
    *,
    timeout: float = 10.0,
    fallback: Optional[T] = None,
    name: str = "",
    **kwargs,
) -> Optional[T]:
    """
    安全调用外部函数，带超时 + 异常捕获。

    Args:
        fn: 要调用的函数
        timeout: 超时秒数（默认 10s）
        fallback: 失败时的返回值
        name: 调用名称（日志用）
        **kwargs: 传给 fn 的参数

    Returns:
        fn 的返回值，或 fallback
    """
    import sys as _sys
    _mod = _sys.modules[__name__]
    call_name = name or fn.__name__ if hasattr(fn, "__name__") else str(fn)[:30]
    start = time.monotonic()

    # 熔断器检查：如果连续失败≥5次且未到解锁时间，直接返回 fallback
    with _circuit_lock:
        if _mod._fail_count >= 5 and time.monotonic() < _mod._fail_unlock_at:
            elapsed = time.monotonic() - start
            logger.debug(f"[safe_call] 🔒 {call_name} 熔断中 (跳过, {_mod._fail_unlock_at-time.monotonic():.0f}s)")
            return fallback

    try:
        future = _EXECUTOR.submit(fn, **kwargs)
        result = future.result(timeout=timeout)
        elapsed = time.monotonic() - start

        # 记录成功，重置熔断
        with _circuit_lock:
            _mod._fail_count = 0
            _mod._fail_unlock_at = 0.0

        with _stats_lock:
            s = _call_stats.setdefault(call_name, {"ok": 0, "fail": 0, "total_ms": 0})
            s["ok"] += 1
            s["total_ms"] += int(elapsed * 1000)

        return result

    except FutureTimeout:
        elapsed = time.monotonic() - start
        logger.warning(f"[safe_call] ⏱ {call_name} 超时 ({elapsed:.1f}s > {timeout}s)")
        with _stats_lock:
            s = _call_stats.setdefault(call_name, {"ok": 0, "fail": 0, "total_ms": 0})
            s["fail"] += 1
            s["total_ms"] += int(elapsed * 1000)
        # 记录失败到熔断器
        _record_failure()
        return fallback

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.warning(f"[safe_call] ❌ {call_name} 失败 ({elapsed:.1f}s): {e}")
        with _stats_lock:
            s = _call_stats.setdefault(call_name, {"ok": 0, "fail": 0, "total_ms": 0})
            s["fail"] += 1
            s["total_ms"] += int(elapsed * 1000)
        _record_failure()
        return fallback


def _record_failure():
    """记录一次失败，累计 5 次后触发熔断"""
    global _fail_count, _fail_unlock_at
    with _circuit_lock:
        _fail_count += 1
        if _fail_count >= 5:
            _fail_unlock_at = time.monotonic() + 30.0
            logger.warning(f"[safe_call] 🔴 熔断器触发！30 秒内跳过所有外部 API 调用")
            # 30 秒后自动半开
            import threading
            threading.Timer(30.0, _half_open).start()


def _half_open():
    """半开熔断器"""
    global _fail_count, _fail_unlock_at
    with _circuit_lock:
        _fail_count = max(0, _fail_count - 3)  # 半开：减少计数但不清零


def retry_call(
    fn: Callable[..., T],
    *,
    timeout: float = 10.0,
    retries: int = 2,
    fallback: Optional[T] = None,
    name: str = "",
    **kwargs,
) -> Optional[T]:
    """
    带重试的安全调用。
    重试间隔: 1s
    """
    call_name = name or fn.__name__ if hasattr(fn, "__name__") else str(fn)[:30]
    last_err = None
    for attempt in range(1 + retries):
        result = safe_call(fn, timeout=timeout, fallback=None, name=f"{call_name}#{attempt}", **kwargs)
        if result is not None:
            return result
        if attempt < retries:
            time.sleep(1.0)
    return fallback


def get_stats() -> list[dict]:
    """获取调用统计"""
    with _stats_lock:
        return [
            {"name": k, "ok": v["ok"], "fail": v["fail"], "total_ms": v["total_ms"]}
            for k, v in sorted(_call_stats.items())
        ]


def reset_stats():
    """重置统计"""
    with _stats_lock:
        _call_stats.clear()
