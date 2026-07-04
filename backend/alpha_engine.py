"""
alpha_engine.py  —  Alpha 捕获引擎（全市场动态版）

三层漏斗：
  1. 市场大过滤器 (MarketState → 进攻/防守/存量)
  2. 板块周期过滤器 (动态注册表 → 只保留启动/强化期)
  3. 个股赔率排序器 (Odds Ranker → Top N)

Portfolio Guard:
  输入持仓列表 → sector_scanner 注册表映射板块 → 三档健康评分

废弃了硬编码的 SECTOR_ETF_MAP，改用 sector_scanner 的动态注册表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import numpy as np

from sector_scanner import get_registry
from sector_worker import get_all_lifecycles


EXACT_STOCK_SECTOR_MAP: dict[str, str] = {
    # 通信/科技
    "600941": "通信", "600703": "通信", "688396": "通信",
    "300308": "通信", "300394": "通信", "300502": "CPO",
    "603803": "通信", "601728": "通信",
    # 新能源
    "002594": "新能源车", "300750": "新能源车", "300274": "光伏",
    "601012": "光伏", "600438": "光伏", "002459": "光伏",
    "300124": "机器人", "300014": "锂电池",
    # 证券
    "600030": "证券", "601211": "证券", "600837": "证券",
    "601688": "证券", "601878": "证券", "002736": "证券",
    "300059": "证券", "601555": "证券",
    # 红利（高股息）
    "601088": "煤炭", "600900": "电力", "600519": "白酒",
    "000333": "家电", "000002": "房地产", "601006": "交通运输",
    "601398": "银行", "601288": "银行",
    # 医药
    "600276": "创新药", "300760": "医疗器械", "300015": "创新药",
    "000661": "创新药", "002007": "创新药", "603259": "创新药",
    "300122": "生物医药", "300558": "创新药",
    # 计算机/AI
    "000977": "数据要素", "600570": "数据要素", "300033": "数据要素",
    "603019": "算力", "002230": "人工智能", "300418": "AI应用",
    "688111": "人工智能", "688041": "芯片",
    # 半导体
    "688981": "半导体", "688012": "半导体", "603501": "半导体",
    "300661": "芯片", "002371": "芯片", "688072": "芯片",
    "300782": "芯片",
    # 消费电子
    "002475": "消费电子", "601138": "消费电子", "002241": "消费电子",
    "300433": "消费电子", "688036": "消费电子",
    # 军工
    "600760": "国防军工", "600893": "国防军工", "600038": "低空经济",
    "000768": "国防军工", "600862": "国防军工",
    # 游戏/传媒
    "300418": "游戏", "002624": "游戏", "002555": "游戏",
    # 银行/金融
    "600036": "银行", "000001": "银行", "002142": "银行",
    # 大消费
    "000858": "白酒", "002304": "白酒", "000568": "白酒",
    "600887": "食品饮料", "600690": "家电",
    "002714": "农业", "000876": "农业",
    # 汽车
    "600104": "汽车", "000625": "汽车", "601238": "汽车",
    "002920": "智能驾驶",
    # 保险
    "601318": "保险", "601628": "保险",
    # 房地产
    "001979": "房地产", "600048": "房地产",
    # 电力
    "601985": "核电", "600886": "电力", "600011": "电力",
    # 煤炭
    "601225": "煤炭", "600985": "煤炭",
}


# ── 股票→板块本地映射表（JSON，覆盖范围取决于随项目提交的数据快照）
STOCK_SECTOR_MAP: dict[str, str] = {}

def _load_sector_map():
    """加载 STOCK_SECTOR_MAP.json 精确映射表"""
    global STOCK_SECTOR_MAP
    try:
        import json, os
        path = os.path.join(os.path.dirname(__file__), "STOCK_SECTOR_MAP.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        STOCK_SECTOR_MAP = data.get("mapping", {})
        print(f"[Loading] 加载本地行业映射: {len(STOCK_SECTOR_MAP)} 只股票", flush=True)
    except Exception as e:
        print(f"[Loading] 加载行业映射失败: {e}", flush=True)

def _query_stock_sector(code: str, name: str = "") -> str:
    """优先查本地行业映射表；缺失时使用名称关键词兜底，结果仍需人工复核。"""
    if code in STOCK_SECTOR_MAP:
        return STOCK_SECTOR_MAP[code]
    # 兜底：用名称关键词快速匹配
    name_kw = {
        "银行": "银行", "商行": "银行",
        "证券": "证券", "券商": "证券",
        "保险": "保险",
        "地产": "房地产", "房产": "房地产",
        "酒": "白酒", "茅台": "白酒", "酒鬼": "白酒",
        "食品": "食品饮料", "乳": "食品饮料", "伊利": "食品饮料",
        "药业": "医药", "制药": "医药", "医药": "医药",
        "医疗器械": "医疗器械", "医疗": "医疗器械",
        "生物": "生物医药",
        "光伏": "光伏", "隆基": "光伏",
        "电池": "锂电池", "锂电": "锂电池",
        "新能源": "新能源",
        "电力": "电力", "能源": "电力",
        "煤炭": "煤炭",
        "钢铁": "钢铁", "钢": "钢铁",
        "有色": "有色金属", "金属": "有色金属",
        "铝": "有色金属", "铜": "有色金属", "金": "有色金属",
        "化工": "石油化工", "化": "石油化工",
        "通信": "通信", "中兴": "通信", "移动": "通信",
        "科技": "计算机", "软件": "计算机",
        "军工": "国防军工", "航天": "国防军工",
        "航空": "国防军工", "舰": "国防军工",
        "汽车": "汽车", "重汽": "汽车",
        "交通": "交通运输", "物流": "交通运输", "运": "交通运输",
        "建": "建筑", "工程": "建筑",
        "电": "家电", "格": "家电", "海": "家电",
        "服装": "纺织服装", "纺织": "纺织服装",
        "农业": "农业", "农": "农业",
        "游戏": "游戏",
        "电气": "家电",
        "机器人": "机器人", "自动": "机器人", "数控": "工业母机",
        "芯片": "芯片", "半导体": "半导体",
        "消费": "大消费", "零售": "大消费",
        "旅游": "旅游酒店", "酒店": "旅游酒店",
        "环保": "环保", "环境": "环保",
        "造纸": "造纸",
        "水泥": "水泥",
        "教育": "教育",
        "检测": "检测",
        "互联": "通信", "网络": "计算机",
        "数据": "数据要素", "算": "算力",
        "传媒": "传媒", "广告": "传媒",
        "概念": "其他",
    }
    if name:
        clean = name.replace(" ", "").replace("\\u3000", "")
        for kw, sec in name_kw.items():
            if kw in clean:
                return sec
    print(f"[WARN] 股票 {code} ({name}) 不在映射表中", flush=True)
    return "其他"

# 首次加载
_load_sector_map()

def _map_stock_to_sector(code: str, name: str) -> str:
    """将个股映射到板块（本地映射表 + _query_stock_sector兜底）"""
    return _query_stock_sector(code, name)

@dataclass
class StockCandidate:
    code: str
    name: str
    price: float
    change_pct: float
    sector: str
    sector_phase: str
    sector_confidence: float
    odds_ratio: float
    odds_rating: str
    upside_pct: float
    downside_pct: float
    adjusted_odds: float  # 环境系数 × 周期系数修正后
    score: float          # 最终综合打分
    rank: int = 0

@dataclass
class PortfolioItem:
    code: str
    name: str
    sector: str
    sector_phase: str
    sector_confidence: float
    health: str  # "healthy" | "inefficient" | "at_risk"
    reason: str
    signal: dict = field(default_factory=dict)


# ── 辅助函数 ──

def _fetch_stock_data(code: str) -> Optional[dict]:
    """获取单只个股最近100日行情用于赔率计算"""
    # 确定交易所前缀
    if code.startswith(("6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    symbol = f"{prefix}{code}"
    from network_guard import safe_call
    try:
        df = safe_call(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=(datetime.now() - timedelta(days=200)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",
            ),
            timeout=8.0, name=f"alpha_hist_{code}"
        )
        if df is None or (hasattr(df, "empty") and df.empty):
            return None
        return {
            "df": df,
            "current_price": float(df["close"].iloc[-1]) if "close" in df.columns else float(df["收盘"].iloc[-1]),
            "latest": df.iloc[-1],
        }
    except Exception as e:
        print(f"[Alpha] {code} 数据获取失败: {e}", flush=True)
        return None


def _compute_odds_for_stock(data: dict) -> dict:
    """
    简化版个股赔率计算（无需 main.py 中 compute_odds 的全量指标，
    这里只使用价格序列计算上行/下行赔率）
    """
    df = data["df"]
    close_col = "close" if "close" in df.columns else "收盘"
    closes = df[close_col].astype(float).values
    n = len(closes)
    if n < 20:
        return {"odds_ratio": 1.0, "rating": "低赔率", "upside_pct": 0, "downside_pct": 0}

    # 近20日高低
    high_20 = max(closes[-20:])
    low_20 = min(closes[-20:])
    current = closes[-1]

    # 上行空间: 到近20日高点的距离百分比
    upside = max(0, (high_20 - current) / current * 100)
    # 下行空间: 到近20日低点的距离百分比
    downside = max(0, (current - low_20) / current * 100)

    # 赔率比（ATR 感知版）
    diffs = np.diff(closes)
    atr = np.mean(np.abs(diffs[-min(14, len(diffs)):]))

    # 波动率调整后的上行/下行概率
    vol = np.std(diffs[-min(20, len(diffs)):]) if len(diffs) >= 20 else atr
    effective_upside = upside / max(vol / current * 100, 1)
    effective_downside = downside / max(vol / current * 100, 1)

    odds = effective_upside / max(effective_downside, 0.1)

    def floor_to(n: float, precision: float = 0.01) -> float:
        return round(max(n, 0), 2)

    rating = "高赔率" if odds >= 2.0 else ("合理" if odds >= 1.0 else "低赔率")
    return {
        "odds_ratio": floor_to(odds),
        "rating": rating,
        "upside_pct": floor_to(upside),
        "downside_pct": floor_to(downside),
    }


# ── 核心 ──

class AlphaEngine:
    """
    Alpha 捕获引擎。

    构造后调用 run() 一次性获取完整信号：
      - 市场状态（由外部注入）
      - 板块生命周期（由外部注入）
      - 个股筛选池
      - 个股精选（Top N）
    """

    def __init__(
        self,
        market_state: Optional[dict] = None,
        sector_lifecycles: Optional[dict] = None,
    ):
        self.market_state = market_state or {}
        self.sector_lifecycles = sector_lifecycles or {}
        self._sectors: dict[str, dict] = self.sector_lifecycles or {}

    # ── 环境系数（第二层修正） ──
    def _market_factor(self) -> dict:
        env = self.market_state.get("environment", "range_bound")
        adj = self.market_state.get("adjustment_factor", 1.0)
        label = self.market_state.get("label", "震荡")
        return {"environment": env, "adjustment_factor": adj, "label": label}

    def _phase_factor(self, sector_name: str) -> float:
        """
        板块周期阶段 → 额外系数
        强化期 (strengthening): x1.2 — 趋势加持
        启动期 (initiation):    x1.1 — 刚转势，稍保守
        分歧期 (divergence):    x0.7 — 分歧加大，压低
        退潮期 (decay):         x0.3 — 建议回避
        """
        info = self._sectors.get(sector_name, {})
        phase = info.get("phase", "noise")
        factors = {
            "strengthening": 1.2,
            "initiation": 1.1,
            "startup": 1.1,
            "main_rise_1": 1.2,
            "acceleration": 1.0,
            "divergence": 0.7,
            "high_divergence": 0.7,
            "ice_recovery": 0.8,
            "decay": 0.3,
            "noise": 0.5,
        }
        return factors.get(phase, 1.0)

    def _is_entry_phase(self, phase: str) -> bool:
        """兼容旧 4 阶段与当前 6 阶段生命周期。"""
        return phase in ("initiation", "strengthening", "startup", "main_rise_1", "acceleration")

    def _is_watch_phase(self, phase: str) -> bool:
        return phase in ("divergence", "high_divergence", "ice_recovery")

    def _stocks_for_sector(self, sector_name: str) -> list[str]:
        stocks = [code for code, sec in EXACT_STOCK_SECTOR_MAP.items() if sec == sector_name]
        if not stocks:
            stocks = [code for code, sec in STOCK_SECTOR_MAP.items() if sec == sector_name]
        return sorted(set(stocks))

    def _map_stock_to_sector(self, code: str, name: str) -> str:
        """将个股映射到板块（JSON精确映射表 + 模块级兜底）"""
        return _query_stock_sector(code, name)

    def compute_stock_score(
        self, candidate: StockCandidate
    ) -> tuple[float, StockCandidate]:
        """
        综合打分公式:

        score = odds_ratio × adjustment_factor × phase_factor
              - (板块退潮时的大额惩罚)

        归一化后输出 0-100 分
        """
        mf = self._market_factor()
        pf = self._phase_factor(candidate.sector)

        # 基础分 = 赔率 × 环境系数 × 周期系数
        base = candidate.odds_ratio * mf["adjustment_factor"] * pf

        # 板块退潮期：额外惩罚（即使个股赔率高，板块在退潮也要压低）
        if candidate.sector_phase == "decay":
            base *= 0.3
        elif candidate.sector_phase == "divergence":
            base *= 0.7

        # 归一化到 0-100 (假设最大赔率5倍)
        score = min(round(base / 5.0 * 100, 1), 100)

        candidate.adjusted_odds = round(
            candidate.odds_ratio * mf["adjustment_factor"] * pf, 3
        )
        candidate.score = score
        return score, candidate

    def run(
        self, top_n: int = 10
    ) -> dict:
        """
        执行完整 Alpha 筛选流程

        返回:
          "candidates": StockCandidate[]
          "full_screener": 筛选结果摘要
        """
        mf = self._market_factor()

        # ── Step 1: 确定筛选板块（只从强化/启动期的板块中选股） ──
        target_sectors = []
        for name, info in self._sectors.items():
            phase = info.get("phase", "noise")
            if self._is_entry_phase(phase):
                target_sectors.append((name, info.get("confidence", 0.5)))
        # 按置信度降序
        target_sectors.sort(key=lambda x: -x[1])

        # 如果没有任何板块处于启动/强化期，退而求其次用分歧期
        if not target_sectors:
            for name, info in self._sectors.items():
                phase = info.get("phase", "noise")
                if self._is_watch_phase(phase):
                    target_sectors.append((name, 0.3))

        all_candidates: list[StockCandidate] = []

        # ── Step 2: 遍历目标板块 → 取成分股 → 算赔率 → 打分 ──
        # 使用精确映射表 + 注册表获取板块对应的成分股
        for sector_name, _ in target_sectors:
            phase_info = self._sectors.get(sector_name, {})
            sector_phase = phase_info.get("phase", "noise")
            sector_confidence = phase_info.get("confidence", 0)

            # 从 EXACT_STOCK_SECTOR_MAP 中取属于该板块的股票代码
            mapped_stocks = self._stocks_for_sector(sector_name)

            for stock_code in mapped_stocks:
                stock_data = _fetch_stock_data(stock_code)
                if stock_data is None:
                    continue
                try:
                    odds = _compute_odds_for_stock(stock_data)
                    candidate = StockCandidate(
                        code=stock_code,
                        name=stock_code,
                        price=stock_data["current_price"],
                        change_pct=0,
                        sector=sector_name,
                        sector_phase=sector_phase,
                        sector_confidence=sector_confidence,
                        odds_ratio=odds["odds_ratio"],
                        odds_rating=odds["rating"],
                        upside_pct=odds["upside_pct"],
                        downside_pct=odds["downside_pct"],
                        adjusted_odds=0,
                        score=0,
                    )
                    score, candidate = self.compute_stock_score(candidate)
                    all_candidates.append(candidate)
                except Exception:
                    continue

        # ── Step 3: 排序 + 截断 ──
        all_candidates.sort(key=lambda x: -x.score)
        for i, c in enumerate(all_candidates):
            c.rank = i + 1

        top_candidates = all_candidates[:top_n]

        # ── 格式化输出 ──
        screener_result = []
        for c in top_candidates:
            screener_result.append({
                "rank": c.rank,
                "code": c.code,
                "name": c.name,
                "price": c.price,
                "sector": c.sector,
                "sector_phase": c.sector_phase,
                "odds_ratio": c.odds_ratio,
                "adjusted_odds": c.adjusted_odds,
                "score": c.score,
            })

        return {
            "market_state": mf,
            "target_sectors": [s for s, _ in target_sectors],
            "total_candidates_scanned": len(all_candidates),
            "candidates": top_candidates[:top_n],
            "screener": screener_result,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 通过 ETF 成分股 + 预置龙头名单补充 ──
    # _get_sector_stocks 已废弃 — AlphaEngine 改用 EXACT_STOCK_SECTOR_MAP 直接映射
    # _get_stock_name 已废弃 — 名称由前端从实时行情获取


# ── Portfolio Guard ──



class PortfolioGuard:
    """
    持仓诊断引擎。

    输入: 持仓列表 [{code, name, weight?}]
    输出: 每个持仓的健康评分 + 板块映射 + 操作建议
    """

    def __init__(
        self,
        positions: list[dict],
        sector_lifecycles: dict,
        market_state: Optional[dict] = None,
    ):
        self.positions = positions
        self.sector_lifecycles = sector_lifecycles or {}
        self.market_state = market_state or {}
        self._sectors: dict[str, dict] = self.sector_lifecycles or {}

    def run(self) -> list[dict]:
        results: list[PortfolioItem] = []
        mf_env = self.market_state.get("environment", "range_bound")
        registry = get_registry()
        all_sector_names = registry.get_sector_names()

        for pos in self.positions:
            code = pos.get("code", "")
            name = pos.get("name", code)

            # 精确映射表优先
            mapped_sector = EXACT_STOCK_SECTOR_MAP.get(code)
            if not mapped_sector:
                # 兜底：在注册表所有板块名中搜索
                for sec_name in all_sector_names:
                    if sec_name in name or name in sec_name:
                        mapped_sector = sec_name
                        break
            if not mapped_sector:
                # 终极兜底：akshare查询
                try:
                    mapped_sector = _query_stock_sector(code, name)
                except Exception:
                    pass

            if mapped_sector:
                sector_info = self._sectors.get(mapped_sector, {})
                phase = sector_info.get("phase", "noise")
                confidence = sector_info.get("confidence", 0)

                risk_on_amplifier = 1.2 if mf_env == "risk_off" else 1.0

                if phase == "decay":
                    health = "at_risk"
                    reason = f"所属板块({mapped_sector})处于退潮期，大盘环境{self.market_state.get('label','震荡')}，建议清仓"
                elif phase == "divergence":
                    if risk_on_amplifier > 1.0:
                        health = "at_risk"
                        reason = f"板块在防守市中分歧，风险加剧，清仓为宜"
                    else:
                        health = "inefficient"
                        reason = f"所属板块({mapped_sector})处于分歧期，波动加大，建议减仓或上移止损"
                elif phase == "strengthening" or phase == "initiation":
                    health = "healthy"
                    reason = f"所属板块({mapped_sector})处于{phase}，核心持仓"
                else:
                    health = "inefficient"
                    reason = f"所属板块({mapped_sector})无明显趋势({phase})，等待方向明确"

                results.append(PortfolioItem(
                    code=code,
                    name=name,
                    sector=mapped_sector,
                    sector_phase=phase,
                    sector_confidence=confidence,
                    health=health,
                    reason=reason,
                ))
            else:
                # 未匹配到板块 → 无法判定
                results.append(PortfolioItem(
                    code=code,
                    name=name,
                    sector="未知",
                    sector_phase="noise",
                    sector_confidence=0,
                    health="inefficient",
                    reason="无法映射到已知板块，需要手动分析",
                ))

        sorted_p = sorted(results, key=lambda x: (
            0 if x.health == "at_risk" else 1 if x.health == "inefficient" else 2
        ))

        return [
            {
                "code": p.code,
                "name": p.name,
                "sector": p.sector,
                "sector_phase": p.sector_phase,
                "sector_confidence": p.sector_confidence,
                "health": p.health,
                "reason": p.reason,
            }
            for p in sorted_p
        ]


# ── 执行摘要生成 ──

def generate_executive_summary(
    market_state: dict,
    sector_lifecycles: dict,
    alpha_result: Optional[dict] = None,
) -> str:
    """
    生成"投委会级"每日执行摘要。
    不调用 LLM，纯规则引擎生成 -> 冷峻、毒蛇、不留情面。
    """
    env = market_state.get("environment", "range_bound")
    label = market_state.get("label", "震荡")
    factor = market_state.get("adjustment_factor", 1.0)
    main_line = market_state.get("main_line", "无")
    volume_trend = market_state.get("volume_trend", "持平")

    sectors: dict[str, dict] = sector_lifecycles or {}

    summary_parts: list[str] = [
        f"📊 市场状态: {label} (系数×{factor})"
    ]
    summary_parts.append(f"   成交量{volume_trend} | {main_line}")

    # 板块信号 — 适配 6 阶段生命周期
    # 6 阶段: startup / main_rise_1 / acceleration / high_divergence / decay / ice_recovery
    startup_sectors = [n for n, i in sectors.items() if i.get("phase") == "startup"]
    main_rise_sectors = [n for n, i in sectors.items() if i.get("phase") == "main_rise_1"]
    accel_sectors = [n for n, i in sectors.items() if i.get("phase") == "acceleration"]
    high_div_sectors = [n for n, i in sectors.items() if i.get("phase") == "high_divergence"]
    decay_sectors = [n for n, i in sectors.items() if i.get("phase") == "decay"]
    ice_recovery_sectors = [n for n, i in sectors.items() if i.get("phase") == "ice_recovery"]

    if main_rise_sectors or accel_sectors:
        bullish = main_rise_sectors + accel_sectors
        summary_parts.append(f"\n✅ 强势主线: {', '.join(bullish[:6])} — 主升/加速期，顺势做多")
    if startup_sectors:
        summary_parts.append(f"\n👀 启动关注: {', '.join(startup_sectors[:4])} — 资金回流，启动迹象")
    if high_div_sectors:
        summary_parts.append(f"\n⚠️ 高位分歧: {', '.join(high_div_sectors[:4])} — 注意止盈，控制仓位")
    if ice_recovery_sectors:
        summary_parts.append(f"\n🧊 冰点修复: {', '.join(ice_recovery_sectors[:4])} — 超跌企稳，左侧观察")
    if decay_sectors:
        summary_parts.append(f"\n🔴 退潮信号: {', '.join(decay_sectors[:4])} — 资金系统性退出，回避")

    # 行动建议
    advice: list[str] = []
    if env == "risk_on":
        if main_rise_sectors or accel_sectors:
            bullish = main_rise_sectors + accel_sectors
            advice.append(f"积极进攻，主仓位配置{'/'.join(bullish[:3])}板块龙头")
        elif startup_sectors:
            advice.append(f"试探性建仓{'/'.join(startup_sectors[:3])}，等待主升确认")
        else:
            advice.append("风险偏好高但无明确主线，快进快出，不宜恋战")
    elif env == "risk_off":
        advice.append("防守模式，降低仓位，聚焦红利/防御板块")
    else:
        advice.append("存量博弈，控制半仓，跟随轮动节奏")

    if decay_sectors:
        advice.append(f"回避{'/'.join(decay_sectors[:3])}板块，不因个股技术面好看而抄底")
    if main_rise_sectors:
        advice.append(f"核心方向: {'/'.join(main_rise_sectors[:3])}板块龙头")

    summary_parts.append(f"\n💡 策略: {' | '.join(advice)}")

    # Alpha 池
    if alpha_result and alpha_result.get("candidates"):
        top = alpha_result["candidates"][:3]
        summary_parts.append(f"\n🎯 今日精选池:")
        for c in top:
            summary_parts.append(
                f"   {c.rank}. {c.code} · {c.sector} · 综合评分{c.score} · 赔率{c.odds_ratio}倍"
            )

    return "\n".join(summary_parts)
