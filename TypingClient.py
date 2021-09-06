from abc import ABC
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, Optional, List

from binance import AsyncClient, BinanceSocketManager
from binance.enums import HistoricalKlinesType

from delegate_attribut import custom_inherit


def to_datetime(binance_datetime: str):
    return datetime.fromtimestamp(int(binance_datetime) / 1000)


class TypingSymbolInfo(dict):
    """ Un client Binance, enrichie de typage fort, lorsque c'est pertinent."""
    class Filter(ABC):
        def __init__(self, filter: Dict[str, Any]):
            self._filter = filter

    class _Price_filter(Filter):
        @property
        def minPrice(self) -> Decimal:
            return Decimal(self._filter["minPrice"])

        @property
        def maxPrice(self) -> Decimal:
            return Decimal(self._filter["maxPrice"])

        @property
        def tickSize(self) -> Decimal:
            return Decimal(self._filter["tickSize"])

    class _Percent_price_filter(Filter):
        @property
        def multiplierUp(self) -> Decimal:
            return Decimal(self._filter["multiplierUp"])

        @property
        def multiplierDown(self) -> Decimal:
            return Decimal(self._filter["multiplierDown"])

        @property
        def avgPriceMins(self) -> int:
            return self._filter["avgPriceMins"]

    class _Lot_size_filter(Filter):
        @property
        def minQty(self) -> Decimal:
            return Decimal(self._filter["minQty"])

        @property
        def maxQty(self) -> Decimal:
            return Decimal(self._filter["maxQty"])

        @property
        def stepSize(self) -> Decimal:
            return Decimal(self._filter["stepSize"])

    class _Min_notional_filter(Filter):
        @property
        def minNotional(self) -> Decimal:
            return Decimal(self._filter["minNotional"])

        @property
        def applyToMarket(self) -> bool:
            return self._filter["applyToMarket"]

        @property
        def avgPriceMins(self) -> Decimal:
            return Decimal(self._filter["avgPriceMins"])

    class _Iceberg_parts_filter(Filter):
        @property
        def limit(self) -> int:
            return self._filter["limit"]

    class _Market_lot_size_filter(Filter):
        @property
        def minQty(self) -> Decimal:
            return Decimal(self._filter["minQty"])

        @property
        def maxQty(self) -> Decimal:
            return Decimal(self._filter["maxQty"])

        @property
        def stepSize(self) -> Decimal:
            return Decimal(self._filter["stepSize"])

    class _Max_num_order_filter(Filter):
        @property
        def maxNumOrder(self) -> int:
            return self._filter["maxNumOrder"]

    class _Max_num_algo_orders_filter(Filter):
        @property
        def maxNumAlgoOrders(self) -> int:
            return self._filter["maxNumAlgoOrders"]

    def __init__(self, symbol_info: Dict[str, Any]):
        super().__init__(symbol_info)

    @property
    def symbol(self) -> str:
        return self["symbol"]

    @property
    def status(self) -> str:
        return self["status"]

    @property
    def baseAsset(self) -> str:
        return self["baseAsset"]

    @property
    def baseAssetPrecision(self) -> int:
        return self["baseAssetPrecision"]

    @property
    def quoteAsset(self) -> str:
        return self["quoteAsset"]

    @property
    def quotePrecision(self) -> int:
        return self["quotePrecision"]

    @property
    def baseCommissionPrecision(self) -> int:
        return self["baseCommissionPrecision"]

    @property
    def quoteCommissionPrecision(self) -> int:
        return self["quoteCommissionPrecision"]

    @property
    def orderTypes(self) -> List[str]:
        return self["orderTypes"]

    @property
    def price(self):
        return TypingSymbolInfo._Price_filter(
            next(filter(lambda x: x["filterType"] == 'PRICE_FILTER', self['filters'])))

    @property
    def percent(self):
        return TypingSymbolInfo._Percent_price_filter(
            next(filter(lambda x: x["filterType"] == 'PERCENT_PRICE', self['filters'])))

    @property
    def lot(self):
        return TypingSymbolInfo._Lot_size_filter(
            next(filter(lambda x: x["filterType"] == 'LOT_SIZE', self['filters'])))

    @property
    def min_notional(self):
        return TypingSymbolInfo._Min_notional_filter(
            next(filter(lambda x: x["filterType"] == 'MIN_NOTIONAL', self['filters'])))

    @property
    def iceberg_parts(self):
        return TypingSymbolInfo._Iceberg_parts_filter(
            next(filter(lambda x: x["filterType"] == 'ICEBERG_PARTS', self['filters'])))

    @property
    def market_lot_size(self):
        return TypingSymbolInfo._Market_lot_size_filter(
            next(filter(lambda x: x["filterType"] == 'MARKET_LOT_SIZE', self['filters'])))

    @property
    def max_num_orders(self):
        return TypingSymbolInfo._Max_num_order_filter(
            next(filter(lambda x: x["filterType"] == 'MAX_NUM_ORDERS', self['filters'])))

    @property
    def max_num_algo_orders(self):
        return TypingSymbolInfo._Max_num_algo_orders_filter(
            next(filter(lambda x: x["filterType"] == 'MAX_NUM_ALGO_ORDERS', self['filters'])))

    @property
    def permissions(self) -> List[str]:
        return self["permissions"]


def _conv_decimal_in_map(keys: List[str], dic: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v if k not in keys else Decimal(v) for k, v in dic.items()}


def _conv_symbolinfo(info:Dict[str,Any]) -> Dict[str,Any]:
    return TypingSymbolInfo(info)

# Une delegation du client Binance, pour convertir - à la demande - les str en Decimal
# et les dates en Date.
# Cette classe va évoluer au fur et à mesure des besoins.
@custom_inherit(AsyncClient, delegator='_delegate')
class TypingClient():
    @classmethod
    async def create(
            cls,
            api_key: Optional[str] = None,
            api_secret: Optional[str] = None,
            **kw
    ):
        return TypingClient(await AsyncClient.create(api_key, api_secret, **kw))

    def __init__(self, delegate: AsyncClient):
        self._delegate = delegate

    def get_socket_manager(self, user_timeout:int=60):
        return BinanceSocketManager(self._delegate, user_timeout=user_timeout)

    async def get_symbol_info(self, symbol: str) -> Dict[str,Any]:
        return _conv_symbolinfo(await self._delegate.get_symbol_info(symbol))

    async def get_recent_trades(self, **params) -> List[Dict[str, Any]]:
        result = await self._delegate.get_recent_trades(**params)
        return [_conv_decimal_in_map(['price', 'qty'], x) for x in result]

    async def get_aggregate_trades(self, **params) -> List[Dict[str, Any]]:
        result = await self._delegate.get_aggregate_trades(**params)
        return [_conv_decimal_in_map(['p', 'q'], x) for x in result]

    async def get_klines(self, **params) -> Dict:
        result = await self._delegate.get_klines(**params)
        return [[Decimal(x) if isinstance(x, str) else x for x in kline] for kline in result]

    async def get_historical_klines(self, symbol, interval, start_str, end_str=None, limit=500,
                                    klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT):
        result = await self._delegate.get_historical_klines(symbol, interval, start_str, end_str, limit, klines_type, )
        return [[Decimal(x) if isinstance(x, str) else x for x in kline] for kline in result]

    async def get_historical_klines_generator(self, symbol, interval, start_str, end_str=None,
                                              klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT):
        result = await self._delegate.get_historical_klines_generator(symbol, interval, start_str, end_str, klines_type)
        return [[Decimal(x) if isinstance(x, str) else x for x in kline] for kline in result]

    async def get_avg_price(self, **params):
        return _conv_decimal_in_map(['price'], await self._delegate.get_avg_price(**params))

    async def get_account(self, **params):
        result = await self._delegate.get_account(**params)
        if result:
            result["balances"] = [_conv_decimal_in_map(["free", "locked"], x) for x in result["balances"]]
        return result

    async def get_asset_balance(self, asset, **params):
        return _conv_decimal_in_map(["free", "locked"], await self._delegate.get_asset_balance(asset=asset, **params))

    async def get_symbol_ticker(self, **params):
        result = await self._delegate.get_symbol_ticker(**params)
        result["price"] = Decimal(result["price"])
        return result
