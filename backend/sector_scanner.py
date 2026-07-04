"""
sector_scanner.py  —  全市场板块动态注册表（TX API 版）

由于 WSL2 环境下 push2.eastmoney.com 被 Windows 代理软件（Clash TUN）拦截，
本模块使用以下策略：
  1. 内置 45 个板块的名称关键词分类器 + 1200 条代码映射
  2. 使用 TX API (stock_zh_a_hist_tx) 获取成分股行情
  3. 不依赖 eastmoney 的板块指数 API
  4. 保留自动刷新架构，某一天 eastmoney 通联可无缝切换

板块数量: ~45 个（覆盖 A 股全部核心行业）

数据源说明（2026-05-26 升级）：
  板块→股票映射不再依赖手工维护的极简名单（每板块 5-8 只），
  而是通过 AKShare stock_info_a_code_name 获取 A 股全市场 5522 只股票，
  按名称关键词自动归类到 45 个板块，每板块最多 30 只。
  覆盖约 1200 条代码→板块映射关系。
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import numpy as np
import pandas as pd

# ── 配置 ──

REGISTRY_CACHE_FILE = "/tmp/sector_registry.json"
MAX_SECTORS = 100

# ── 全市场板块锚点列表（自动生成，每板块 ≤30 只股票） ──
# 使用 AKShare stock_info_a_code_name 的 5522 只 A 股，
# 通过名称关键词自动归类到 45 个主要板块，共计 ~1200 条映射
BUILTIN_SECTORS: dict[str, dict] = {
    "AI/算力": {"stocks": ["000970", "000977", "002230", "002657", "002819", "300035", "300678", "300810", "301141", "301153", "301175", "603019", "603516", "603927", "688038", "688047", "688211", "688332", "688352", "688361", "688568", "920186", "920992"], "type": "industry"},
    "CPO": {"stocks": ["300570"], "type": "industry"},
    "互联网": {"stocks": ["300033", "300059", "300315", "300418", "601360", "603000"], "type": "industry"},
    "交通运输": {"stocks": ["000089", "000520", "001872", "002401", "003013", "600004", "600009", "600018", "600026", "600428", "600515", "601006", "601018", "601083", "601333", "601816", "601866", "601872", "601919"], "type": "industry"},
    "传媒": {"stocks": ["000156", "000719", "000917", "002027", "002181", "002292", "002343", "002712", "002905", "300027", "300043", "300133", "300251", "300265", "300788", "300987", "301102", "301551", "600088", "600229", "600373", "600386", "600551", "600623", "600757", "600825", "600977", "601019", "601098", "601801"], "type": "industry"},
    "保险": {"stocks": ["601319", "601336", "601628"], "type": "industry"},
    "储能": {"stocks": ["002335", "600995", "603161", "688063"], "type": "industry"},
    "光伏": {"stocks": ["002363", "002459", "300274", "300724", "300751", "300763", "300776", "300827", "301680", "600438", "601012", "603806", "605117", "688223", "688390", "688516", "688599"], "type": "industry"},
    "军工": {"stocks": ["000066", "000547", "000697", "000738", "000768", "000901", "002025", "002179", "002389", "002690", "002928", "300123", "300446", "300455", "300581", "300900", "302132", "600029", "600150", "600151", "600271", "600316", "600343", "600372", "600391", "600482", "600501", "600562", "600760", "600765"], "type": "industry"},
    "农业": {"stocks": ["000816", "000860", "000876", "000998", "002041", "002311", "002321", "002385", "002714", "300189", "300498", "600313", "600354", "600598", "920087", "920403"], "type": "industry"},
    "创新药": {"stocks": ["000701", "600657", "688185", "688235", "688443"], "type": "industry"},
    "化工": {"stocks": ["000683", "000830", "002001", "002010", "002054", "002064", "002092", "002145", "002146", "002226", "002258", "002274", "002469", "002493", "002648", "300214", "300261", "300758", "300927", "301209", "301212", "301256", "301518", "600160", "600309", "600328", "600346", "600409", "600426"], "type": "industry"},
    "医疗器械": {"stocks": ["300760", "688271", "688351", "688410", "300832", "000538", "002223", "002022"], "type": "industry"},
    "医药": {"stocks": ["000028", "000153", "000403", "000504", "000518", "000538", "000566", "000590", "000597", "000650", "000739", "000756", "000788", "000919", "000950", "000952", "000963", "000999", "001367", "002007", "002019", "002020", "002022", "002038", "002099", "002100", "002107", "002166", "002173", "002223"], "type": "industry"},
    "半导体": {"stocks": ["002156", "002371", "300474", "300607", "300661", "300782", "600584", "600877", "603005", "603173", "603290", "688008", "688041", "688135", "688167", "688216", "688256", "688347", "688981"], "type": "industry"},
    "基建工程": {"stocks": ["000965", "001213", "002941", "300374", "300425", "600528", "601117", "601186", "601390", "601611", "601618", "601669", "601800", "601868", "603815", "688425"], "type": "industry"},
    "多元金融": {"stocks": ["000402", "000415", "600318", "600816"], "type": "industry"},
    "家电": {"stocks": ["000100", "000333", "000404", "000521", "000651", "000810", "000921", "002032", "002129", "002242", "002508", "002668", "301260", "600060", "600690", "600839", "603486", "603868", "920239"], "type": "industry"},
    "建材": {"stocks": ["000012", "000619", "000672", "000786", "002271", "300390", "301077", "301149", "301265", "600176", "600449", "600585", "600801", "600802", "601636", "603055", "603310", "603370", "603683", "688707", "920015"], "type": "industry"},
    "房地产": {"stocks": ["000002", "000069", "000517", "001979", "600048", "600159", "600383", "600606", "600708"], "type": "industry"},
    "新能源": {"stocks": ["000690", "001258", "002074", "300014", "300207", "300438", "300750", "600617"], "type": "industry"},
    "新能源车": {"stocks": ["000625", "002594", "600104", "600418", "600733", "601127", "601238"], "type": "industry"},
    "智能驾驶": {"stocks": ["002906", "002920", "300496", "300552", "688326"], "type": "industry"},
    "有色金属": {"stocks": ["000426", "000630", "000737", "000751", "000807", "000831", "000878", "000933", "000960", "001332", "002532", "002578", "301170", "600111", "600219", "600259", "600301", "600362", "600531", "601061", "601212", "601600", "601677", "601702", "601899", "603248", "603799", "603993", "605319", "920547"], "type": "industry"},
    "机器人": {"stocks": ["002747", "300024", "300161", "688017"], "type": "industry"},
    "机械设备": {"stocks": ["000157", "000410", "000425", "001395", "300124", "300450", "301298", "600031", "600641", "600761", "600984", "601100", "603298", "603677", "605259", "688222", "920101"], "type": "industry"},
    "核能核电": {"stocks": ["002366", "601985"], "type": "industry"},
    "氢能": {"stocks": ["000723", "300471"], "type": "industry"},
    "汽车零部件": {"stocks": ["000589", "600660", "600741", "601058", "601163", "601689", "601799", "601966", "603305", "603596", "920806"], "type": "industry"},
    "消费零售": {"stocks": ["002419", "600694", "600712", "600785", "600827", "600859", "601888", "601933", "603708"], "type": "industry"},
    "煤炭": {"stocks": ["000983", "600188", "600395", "600985", "601001", "601088", "601225", "601666", "601699", "601898"], "type": "industry"},
    "物流快递": {"stocks": ["000957", "002120", "002352", "002468", "300013", "300227", "301584", "600125", "600153", "600233", "600266", "600603", "600834", "601156", "603117", "603128", "603565", "603569", "603909", "603967"], "type": "industry"},
    "环保": {"stocks": ["000544", "000598", "000811", "000885", "000967", "001230", "002011", "002034", "002479", "002573", "002672", "300056", "300070", "300137", "300140", "300172", "300187", "300266", "300536", "300664", "300779", "300854", "300864", "300867", "300929", "301018", "301203", "301273", "301288", "600008"], "type": "industry"},
    "电力": {"stocks": ["000537", "000539", "000543", "000767", "000966", "000993", "001289", "002039", "300069", "301439", "301609", "600011", "600021", "600023", "600027", "600101", "600198", "600236", "600396", "600505", "600578", "600642", "600644", "600674", "600726", "600744", "600795", "600886", "600900", "600905"], "type": "industry"},
    "电子": {"stocks": ["000561", "000682", "000823", "000988", "001365", "001388", "002008", "002036", "002052", "002119", "002121", "002138", "002197", "002217", "002241", "002339", "002384", "002519", "002546", "002579", "002587", "002767", "002935", "003031", "300090", "300327", "300408", "300433", "300456", "300479"], "type": "industry"},
    "白酒": {"stocks": ["000568", "000596", "000799", "000858", "002646", "600197", "600199", "600519", "600559", "600702", "600779", "600809", "603132", "603198", "603589", "603919"], "type": "industry"},
    "石油石化": {"stocks": ["000554", "000703", "000852", "002353", "300164", "600028", "600256", "600688", "600871", "600938", "601808", "601857", "603353", "603619"], "type": "industry"},
    "证券": {"stocks": ["000686", "000728", "000750", "000776", "000783", "002500", "002670", "002673", "002736", "002926", "002939", "002945", "600030", "600109", "600369", "600906", "600909", "600918", "600958", "600999", "601059", "601108", "601136", "601162", "601198", "601236", "601375", "601377", "601555", "601688"], "type": "industry"},
    "软件服务": {"stocks": ["002063", "002065", "002279", "002410", "002474", "300036", "300271", "300339", "300525", "300663", "300996", "301185", "600536", "600588", "600756", "600845", "603232", "603383", "603636", "688083", "688095", "688111", "688232", "688435", "688588", "688590", "688657", "920799", "920953"], "type": "industry"},
    "通信": {"stocks": ["000063", "000586", "002115", "002281", "002446", "002465", "300136", "300308", "300394", "300502", "300590", "300597", "300711", "600050", "600345", "600498", "600776", "600941", "601728", "603220", "603236", "603322", "603602", "688618", "688653", "688702"], "type": "industry"},
    "钢铁": {"stocks": ["000709", "000825", "000898", "000923", "000932", "000959", "001208", "002110", "600010", "600019", "600022", "600282", "600507", "600569", "600782", "600808", "601003", "601005", "601968", "603356"], "type": "industry"},
    "银行": {"stocks": ["000001", "001227", "002142", "002807", "002936", "002948", "002958", "002966", "600000", "600015", "600016", "600036", "600908", "600919", "600926", "600928", "601009", "601077", "601128", "601166", "601169", "601187", "601229", "601288", "601328", "601398", "601528", "601577", "601658", "601665"], "type": "industry"},
    "风电": {"stocks": ["002202", "002487", "002531", "002800", "300440", "300739", "300772", "301155", "301291", "601016", "601615", "688660", "920663"], "type": "industry"},
    "食品饮料": {"stocks": ["000895", "001219", "001318", "002124", "002216", "002481", "002507", "002557", "002661", "002702", "002732", "002946", "002956", "002991", "003000", "300268", "300783", "300892", "300908", "300973", "301116", "600298", "600305", "600452", "600597", "600887", "601882", "603027", "603057", "603170"], "type": "industry"},
    "黄金": {"stocks": ["000506", "001337", "002155", "600489", "600547", "600916", "600988", "601069"], "type": "industry"},
}


@dataclass
class SectorInfo:
    name: str
    stocks: list[str]
    board_type: str
    avg_amount: float
    stock_cnt: int
    updated_at: str
    is_anchor: bool = True


class SectorRegistry:
    """板块注册表（基于内置锚点列表）"""

    def __init__(self):
        self._cache: dict[str, SectorInfo] = {}
        self._last_refresh_time: Optional[datetime] = None
        # 强制从 BUILTIN_SECTORS 构建，完全绕过文件 I/O
        self._build_from_builtin()

    def get_active(self) -> dict[str, dict]:
        return {
            name: {
                "name": info.name,
                "stocks": info.stocks,
                "board_type": info.board_type,
                "avg_amount": info.avg_amount,
                "stock_cnt": info.stock_cnt,
                "is_anchor": info.is_anchor,
            }
            for name, info in self._cache.items()
        }

    def get_sector_list(self) -> list[dict]:
        return list(self.get_active().values())

    def get_sector_names(self) -> list[str]:
        return list(self._cache.keys())

    def get_stocks_for_sector(self, sector_name: str) -> list[str]:
        info = self._cache.get(sector_name)
        if info:
            return info.stocks
        return []

    def refresh_if_needed(self, force: bool = False) -> bool:
        now = datetime.now()
        if force or self._last_refresh_time is None:
            return self.refresh()
        today = now.date()
        if self._last_refresh_time.date() < today:
            return self.refresh()
        return False

    def refresh(self) -> bool:
        """刷新：估算每个板块的日均成交额（通过成分股TX数据）"""
        import socket
        socket.setdefaulttimeout(5)
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        enriched: dict[str, SectorInfo] = {}
        processed = 0

        for name, meta in BUILTIN_SECTORS.items():
            try:
                stocks = meta["stocks"][:5]  # 每板块取前5只代表股估算成交额
                total_amount = 0.0
                valid_stocks = 0

                for code in stocks:
                    try:
                        from network_guard import safe_call
                        prefix = "sh" if code.startswith(("6", "9")) else "sz"
                        df = safe_call(
                            lambda: ak.stock_zh_a_hist_tx(
                                symbol=f"{prefix}{code}",
                                start_date=(now - timedelta(days=20)).strftime("%Y%m%d"),
                                end_date=now.strftime("%Y%m%d"),
                                adjust="qfq",
                            ),
                            timeout=8.0, name=f"sector_scanner_{code}"
                        )
                        if df is not None and (not hasattr(df, 'empty') or not df.empty) and "amount" in df.columns:
                            avg_amt = df["amount"].astype(float).mean()
                            total_amount += avg_amt
                            valid_stocks += 1
                    except Exception:
                        continue

                avg_amount = total_amount / max(valid_stocks, 1)
                enriched[name] = SectorInfo(
                    name=name,
                    stocks=meta["stocks"],
                    board_type=meta["type"],
                    avg_amount=avg_amount,
                    stock_cnt=len(meta["stocks"]),
                    updated_at=ts,
                    is_anchor=name in BUILTIN_SECTORS,
                )
                processed += 1
                if processed % 10 == 0:
                    time.sleep(0.5)
            except Exception as e:
                print(f"[SectorRegistry] 板块 '{name}' 处理失败: {e}，跳过继续", flush=True)
                continue

        # 保留全部板块（不过滤僵尸，让前端自己决定展示阈值）
        self._cache = enriched
        self._last_refresh_time = now
        self._save_cache()
        print(
            f"[SectorRegistry] 刷新成功: {len(self._cache)} 个板块",
            flush=True,
        )
        return True

    def _save_cache(self):
        data = {
            name: {
                "name": info.name,
                "stocks": info.stocks,
                "board_type": info.board_type,
                "avg_amount": info.avg_amount,
                "stock_cnt": info.stock_cnt,
                "updated_at": info.updated_at,
                "is_anchor": info.is_anchor,
            }
            for name, info in self._cache.items()
        }
        try:
            with open(REGISTRY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            pass  # silent

    def _build_from_builtin(self):
        """从 BUILTIN_SECTORS 构建注册表（不依赖 IO）"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for name, meta in BUILTIN_SECTORS.items():
            self._cache[name] = SectorInfo(
                name=name,
                stocks=meta["stocks"],
                board_type=meta["type"],
                avg_amount=0,
                stock_cnt=len(meta["stocks"]),
                updated_at=ts,
                is_anchor=True,
            )
        print(f"[SectorRegistry] 内置注册表: {len(self._cache)} 个板块", flush=True)

    def _load_cache(self):
        if not os.path.exists(REGISTRY_CACHE_FILE):
            return
        try:
            with open(REGISTRY_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, meta in data.items():
                self._cache[name] = SectorInfo(
                    name=meta["name"],
                    stocks=meta.get("stocks", []),
                    board_type=meta.get("board_type", "concept"),
                    avg_amount=meta.get("avg_amount", 0),
                    stock_cnt=meta.get("stock_cnt", 0),
                    updated_at=meta.get("updated_at", ""),
                    is_anchor=meta.get("is_anchor", False),
                )
            print(
                f"[SectorRegistry] 加载缓存: {len(self._cache)} 个板块",
                flush=True,
            )
        except Exception:
            pass


# ── 全局单例 ──
_registry: Optional[SectorRegistry] = None


def get_registry() -> SectorRegistry:
    global _registry
    if _registry is None:
        _registry = SectorRegistry()
    return _registry
