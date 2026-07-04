"""
alpha/scanner.py — Alpha 个股扫描引擎

核心逻辑：
  WorldState + StrategySelector → CandidateStocks

流程：
  1. 获取全市场实时快照（AKShare）
  2. 对快照做板块归属计算（基于 sector_worker 的数据）
  3. 按激活策略分流扫描
  4. 每个策略使用对应 filter
  5. 统一打分排序 + 解释

不依赖 LLM，纯结构化规则引擎。
"""

from __future__ import annotations

import logging
from datetime import datetime

from alpha.market_filter import fetch_market_snapshot
from alpha.filters import (
    filter_trend_breakout,
    filter_sector_rotation,
    filter_dip_stabilization,
    filter_oversold_reversal,
)
from alpha.ranking_v2 import compute_score as ranking_v2
from alpha.data_enrichment import enrich_batch, EnrichedStockSnapshot
from schemas.alpha_candidate import AlphaCandidate

logger = logging.getLogger(__name__)

# 策略名 → filter 函数映射
STRATEGY_FILTER_MAP = {
    "trend_breakout": filter_trend_breakout,
    "sector_rotation": filter_sector_rotation,
    "dip_stabilization": filter_dip_stabilization,
    "oversold_reversal": filter_oversold_reversal,
}

# 策略名 → 展示名
STRATEGY_DISPLAY = {
    "trend_breakout": "趋势突破",
    "sector_rotation": "板块轮动",
    "dip_stabilization": "分歧低吸",
    "oversold_reversal": "超跌反弹",
}

# 板块生命周期中文名
PHASE_CN = {
    "startup": "启动期",
    "main_rise_1": "主升一期",
    "acceleration": "加速期",
    "high_divergence": "高位分歧",
    "decay": "退潮期",
    "ice_recovery": "冰点修复",
}

# 常量（经验阈值）
TOP_SECTOR_DEFAULT = 5  # 前 N 名板块视为领涨
DEFAULT_CANDIDATE_COUNT = 20  # 默认返回候选股数量


# 上次有效的扫描结果缓存（数据源恢复前保留）
_last_valid_candidates: list[AlphaCandidate] = []

def scan(world_state: dict, strategy_result: dict, market_snapshot: list[dict] = None,
         max_candidates: int = 20) -> list[AlphaCandidate]:
    """
    全量扫描入口

    world_state: WorldState Pydantic model 的 dict 序列化
    strategy_result: strategy_selector() 的输出
    market_snapshot: 可选，可复用外部加载的快照
    """
    global _last_valid_candidates

    if market_snapshot is None:
        market_snapshot = fetch_market_snapshot(max_stocks=2000)

    if not market_snapshot:
        logger.warning("[Scanner] No market data available")
        if _last_valid_candidates:
            logger.info(f"[Scanner] 回落至上次有效缓存: {len(_last_valid_candidates)} 只候选")
            return _last_valid_candidates
        return []

    # ═══════════════════════════════════════════════════════
    # ★ 铁血风控：fallback 数据 → 物理截断归零
    # ═══════════════════════════════════════════════════════
    total_valid_stocks = sum(
        1 for s in market_snapshot
        if not s.get("is_fallback", False) and s.get("price", 0) > 0
    )
    is_fallback_all = total_valid_stocks == 0

    if is_fallback_all:
        # 有之前缓存 → 回落
        if _last_valid_candidates:
            logger.info(f"[Scanner] 硬风控触发，回落至上次有效缓存: {len(_last_valid_candidates)} 只")
            return _last_valid_candidates
        logger.warning("[Scanner] 硬风控触发：无真实价格/成交额，返回空候选")
        return []

    active_strategies = strategy_result.get("active_strategies", [])
    if not active_strategies:
        logger.info("[Scanner] No active strategies, skip scan")
        return []

    # 获取板块数据
    sector_lifecycles = world_state.get("lifecycles", {})
    leading_sectors_names = world_state.get("leading_sectors", [])
    heatmap = world_state.get("sector_heatmap", {})
    rotation_speed = world_state.get("rotation_speed", 0.5)

    # 构建板块上下文索引
    sector_map = _build_sector_map(sector_lifecycles, leading_sectors_names, heatmap)
    if not sector_map:
        # ── 外部感知中断 → 返回空候选列表 ──
        # 不做任何伪随机评分。无需 enrich，因为外部 API 已全断。
        return []

    # 尝试在板块映射中找个股归属（akshare 板块数据可能不完整）
    _attach_sector_info(market_snapshot, sector_map)

    # ── 真实数据增强 ──
    enriched_batch_list = enrich_batch(market_snapshot)
    enriched_map: dict[str, dict] = {}
    for es in enriched_batch_list:
        enriched_map[es.symbol] = es.to_dict()
    logger.info(f"[Scanner] Enriched {len(enriched_map)} stocks")

    # 各策略扫描
    all_candidates: list[AlphaCandidate] = []
    strategies_scanned = set()

    for strategy_name in active_strategies:
        filter_fn = STRATEGY_FILTER_MAP.get(strategy_name)
        if filter_fn is None:
            continue

        strategies_scanned.add(strategy_name)
        display_name = STRATEGY_DISPLAY.get(strategy_name, strategy_name)

        for stock in market_snapshot:
            stock_sector = stock.get("sector", "")
            sector_ctx = sector_map.get(stock_sector, {})

            # 无板块归属股票：用默认中等评分兜底（确保全市场覆盖）
            if not stock_sector or not sector_ctx:
                # 给无板块股票一个中性评分，确保回到候选池但排名不高
                from alpha.ranking_v2 import _compute_generic_components
                ctx_default = {
                    "sector_phase": "unknown",
                    "sector_momentum_rank": 50,
                    "sector_bias": 0,
                    "is_leading": False,
                    "index_change_pct": 0,
                }
                extra = {
                    "relative_strength": 0,
                    "pct_5d": 0,
                    "drawdown_pct": 0,
                    "atr_pct": 0,
                    "volatility": 0,
                    "data_source": stock.get("data_source", "fallback"),
                    "turnover_pct": stock.get("turnover", 0),
                }
                generic = _compute_generic_components(
                    stock.get("change_pct", 0),
                    stock.get("volume", 0) / max(stock.get("turnover", 1), 1),
                    stock.get("amount", 0),
                    "unknown", False,
                    {}, stock, extra,
                )
                raw_score = (
                    generic["strategy_fit"] *
                    generic["sector_strength"] *
                    generic["relative_strength"] *
                    generic["volume_quality"] *
                    generic["momentum_quality"] *
                    generic["risk_reward"] *
                    generic["liquidity_score"]
                )
                score = round(raw_score * 100, 1)
                # 无板块候选也加入 all_candidates（排名靠后）
                all_candidates.append(AlphaCandidate(
                    symbol=stock.get("code", ""),
                    name=stock.get("name", ""),
                    sector=stock_sector,
                    score=score,
                    confidence=0.35,
                    triggered_strategy=strategy_name,
                    triggered_strategy_display=display_name,
                    sector_phase="unknown",
                    sector_phase_cn="其他",
                    data_source=stock.get("data_source", "fallback"),
                    tier="Watchlist",
                    reasons=["全市场股票，板块归属待完善"],
                ))
                continue

            ctx = {
                "sector_phase": sector_ctx.get("phase", "noise"),
                "sector_momentum_rank": sector_ctx.get("momentum_rank", 99),
                "sector_bias": sector_ctx.get("bias", 0),
                "is_leading": stock_sector in leading_sectors_names,
                "index_change_pct": world_state.get("breadth_score", 0),
            }

            # 粗筛：只对有合理信号的进行评分
            if ctx["sector_phase"] in ("noise",):
                continue

            # ── 注入增强数据到 stock ──
            code = stock.get("code", "")
            enriched = enriched_map.get(code, {})
            if enriched:
                stock.update(enriched)

            # ── Ranking V2 多维评分 ──
            ranking_result = ranking_v2(
                stock, strategy_name, world_state, sector_ctx,
            )

            if ranking_result["score"] <= 0:
                # fallback: 旧 filter 兜底
                old_score, old_reasons = filter_fn(stock, ctx)
                if old_score <= 0:
                    continue
                score = min(old_score, 100)
                confidence = round(old_score / 100, 2)
                reasons = old_reasons
                tier = "B" if score >= 35 else "Watchlist"
                risk_reward = 0.0
            else:
                score = ranking_result["score"]
                confidence = ranking_result["confidence"]
                reasons = ranking_result["reasons"]
                tier = ranking_result["tier"]
                risk_reward = ranking_result.get("risk_reward", 0.0)

            candidate = AlphaCandidate(
                symbol=stock.get("code", ""),
                name=stock.get("name", ""),
                sector=stock_sector,
                score=score,
                confidence=confidence,
                triggered_strategy=strategy_name,
                triggered_strategy_display=display_name,
                sector_phase=ctx["sector_phase"],
                sector_phase_cn=PHASE_CN.get(ctx["sector_phase"], ctx["sector_phase"]),
                sector_momentum_rank=ctx["sector_momentum_rank"],
                change_pct=stock.get("change_pct", 0),
                change_pct_5d=stock.get("change_pct_5d", 0),
                volume_ratio=stock.get("volume_ratio", 1.0),
                turnover_rate=stock.get("turnover", 0),
                amount_rank=stock.get("amount_rank", 9999),
                stronger_than_sector=stock.get("stronger_than_sector", False),
                stronger_than_index=stock.get("stronger_than_index", False),
                is_first_pullback=stock.get("is_first_pullback", False),
                is_volume_shrink=stock.get("is_volume_shrink", False),
                is_volume_expand=stock.get("is_volume_expand", False),
                is_breakout=stock.get("is_breakout", False),
                risk_level=world_state.get("risk_level", 0.5),
                reasons=reasons,
                tier=tier,
                risk_reward=risk_reward,
                # Data Enrichment 字段
                atr=stock.get("atr", 0.0),
                atr_pct=stock.get("atr_pct", 0.0),
                volatility=stock.get("volatility", 0.0),
                drawdown_pct=stock.get("drawdown_pct", 0.0),
                data_source=stock.get("data_source", "fallback"),
                liquidity_score=stock.get("liquidity_score", 0.0),
            )
            all_candidates.append(candidate)

    # 按分数降序 + 去重（同一代码只保留最高分策略的候选）
    deduped = _deduplicate(all_candidates)
    # 有板块的优先（来源可靠），同板块情况靠分数
    deduped.sort(key=lambda c: (
        0 if hasattr(c, 'is_known_phase') and c.is_known_phase else 1,
        -c.score,
    ))

    logger.info(
        f"[Scanner] scanned strategies={':'.join(strategies_scanned)} "
        f"raw={len(all_candidates)} deduped={len(deduped)}"
    )

    result = deduped[:max_candidates]
    if result:
        _last_valid_candidates[:] = result
    return result


def _build_sector_map(
    lifecycles: dict, leading_names: list[str], heatmap: dict
) -> dict[str, dict]:
    """构建板块索引：name → {phase, bias, momentum_rank}"""
    sector_map = {}
    leading_set = set(leading_names)

    # 从 lifecycles 获取阶段数据（统一转纯字符串）
    for name, phase in lifecycles.items():
        # phase 可能是 LifecyclePhase 枚举或纯字符串
        if hasattr(phase, 'value'):
            phase_val = phase.value
        elif isinstance(phase, str):
            phase_val = phase
        else:
            phase_val = "noise"
        sector_map[name] = {
            "phase": phase_val,
            "bias": 0.0,
            "momentum_rank": 99,
            "is_leading": name in leading_set,
        }

    # 从 heatmap.top_momentum 获取动量排名
    top_momentum = heatmap.get("top_momentum", [])
    for idx, entry in enumerate(top_momentum):
        name = entry.get("name", "")
        if name in sector_map:
            sector_map[name]["momentum_rank"] = idx

    return sector_map


def _attach_sector_info(stocks: list[dict], sector_map: dict) -> None:
    """
    尝试给每个 stock 挂上板块归属

    优先级：
      1. 缓存/内置数据已有 sector → 不覆写
      2. 无 sector 的实时数据 → 从 SectorRegistry code→sector 映射匹配
    """
    # 已有板块归属的不动（来自缓存/内置池）
    already_have = {stock.get("code", ""): stock.get("sector", "")
                    for stock in stocks if stock.get("sector", "")}

    # 从 SectorRegistry 构建 code→sector 映射
    try:
        from sector_scanner import get_registry
        reg = get_registry()
        reg_cache = getattr(reg, '_cache', {})
        code_to_sector = {}
        for s_name, s_info in reg_cache.items():
            slist = getattr(s_info, 'stocks', []) if not isinstance(s_info, dict) else s_info.get('stocks', [])
            for code in slist:
                code_to_sector[code] = s_name
    except Exception as e:
        logger.warning(f"[Scanner] Cannot build code→sector map: {e}")
        for stock in stocks:
            stock["sector"] = stock.get("sector", "")
        return

    for stock in stocks:
        code = stock.get("code", "")
        # 已有板块的不覆写
        if stock.get("sector", ""):
            continue
        sector = code_to_sector.get(code, "")
        stock["sector"] = sector
        # 只填充默认值（不覆写 data_enrichment 已注入的字段）
        if "volume_ratio" not in stock or stock.get("volume_ratio", 0) == 0:
            stock["volume_ratio"] = 1.0
        if "change_pct_5d" not in stock or stock.get("change_pct_5d", 0) == 0:
            stock["change_pct_5d"] = 0.0
        stock.setdefault("amount_rank", 9999)
        stock.setdefault("stronger_than_sector", False)
        stock.setdefault("stronger_than_index", False)
        stock.setdefault("is_first_pullback", False)
        stock.setdefault("is_volume_shrink", False)
        stock.setdefault("is_volume_expand", False)
        stock.setdefault("is_breakout", False)


def _deduplicate(candidates: list[AlphaCandidate]) -> list[AlphaCandidate]:
    """同一只股只保留最高分候选"""
    best = {}
    for c in candidates:
        key = c.symbol
        if key not in best or c.score > best[key].score:
            best[key] = c
    return list(best.values())
