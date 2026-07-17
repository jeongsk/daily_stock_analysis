# -*- coding: utf-8 -*-
"""
===================================
数据源策略层 - 包初始化
===================================

本包实现策略模式管理多个数据源，实现：
1. 统一的数据获取接口
2. 自动故障切换
3. 防封禁流控策略

数据源优先级（动态调整）：
【配置了 TUSHARE_TOKEN 时】
1. TushareFetcher (Priority 0) - 🔥 最高优先级（动态提升）
2. EfinanceFetcher (Priority 0) - 同优先级
3. AkshareFetcher (Priority 1) - 来自 akshare 库
4. PytdxFetcher (Priority 2) - 来自 pytdx 库（通达信）
5. BaostockFetcher (Priority 3) - 来自 baostock 库
6. YfinanceFetcher (Priority 4) - 来自 yfinance 库

【未配置 TUSHARE_TOKEN 时】
1. EfinanceFetcher (Priority 0) - 最高优先级，来自 efinance 库
2. AkshareFetcher (Priority 1) - 来自 akshare 库
3. PytdxFetcher (Priority 2) - 来自 pytdx 库（通达信）
4. TushareFetcher (Priority 2) - 来自 tushare 库（不可用）
5. BaostockFetcher (Priority 3) - 来自 baostock 库
6. YfinanceFetcher (Priority 4) - 来自 yfinance 库
7. LongbridgeFetcher (Priority 5) - 长桥 OpenAPI（美股/港股兜底）
8. TossFetcher (Priority 6) - 토스증권 OpenAPI（credential-gated opt-in）
   - 仅配置 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 时注册；未注册 IP 会被 403 拒绝（详见该 fetcher 内文档）
   - KR：实时行情由 DataFetcherManager 显式路由为 Toss 优先、Yfinance 兜底
   - KR：日线无显式路由，走通用 Priority 排序兜底路径，YfinanceFetcher（Priority 4，
     KRX 官方收盘价）自然排在 TossFetcher（Priority 6）之前，即 yfinance 首选、
     Toss 仅作日线兜底——原因是 Toss 日线K线返回 KRX+NXT 合并成交价，与 KRX 官方
     收盘价存在偏离（详见该 fetcher 内文档）
   - US：日线由 DataFetcherManager 显式追加为四链之后的最后兜底
   - 上面的 Priority 数字仅影响未显式路由的通用兜底路径（当前为最低优先级）

提示：优先级数字越小越优先，同优先级按初始化顺序排列
"""

from .base import BaseFetcher, DataFetcherManager
from .efinance_fetcher import EfinanceFetcher
from .tencent_fetcher import TencentFetcher
from .akshare_fetcher import AkshareFetcher, is_hk_stock_code
from .tushare_fetcher import TushareFetcher
from .pytdx_fetcher import PytdxFetcher
from .baostock_fetcher import BaostockFetcher
from .yfinance_fetcher import YfinanceFetcher
from .longbridge_fetcher import LongbridgeFetcher
from .finnhub_fetcher import FinnhubFetcher
from .alphavantage_fetcher import AlphaVantageFetcher
from .toss_fetcher import TossFetcher
from .us_index_mapping import is_us_index_code, is_us_stock_code, get_us_index_yf_symbol, US_INDEX_MAPPING

__all__ = [
    'BaseFetcher',
    'DataFetcherManager',
    'EfinanceFetcher',
    'TencentFetcher',
    'AkshareFetcher',
    'TushareFetcher',
    'PytdxFetcher',
    'BaostockFetcher',
    'YfinanceFetcher',
    'LongbridgeFetcher',
    'FinnhubFetcher',
    'AlphaVantageFetcher',
    'TossFetcher',
    'is_us_index_code',
    'is_us_stock_code',
    'is_hk_stock_code',
    'get_us_index_yf_symbol',
    'US_INDEX_MAPPING',
]
