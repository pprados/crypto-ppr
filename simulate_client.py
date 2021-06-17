"""
Simulation de l'API binance, pour pouvoir tester une stratégie sur un historique.

Pour le moment, la simulation de génère pas d'event. Il faut poller.
"""
import logging
import random
import sys
from asyncio import Queue
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, Optional, List, Tuple, Iterable, Generator

from binance import AsyncClient, BinanceSocketManager
from binance.enums import HistoricalKlinesType, ORDER_TYPE_MARKET, SIDE_SELL, ORDER_TYPE_LIMIT, SIDE_BUY, \
    ORDER_STATUS_FILLED, ORDER_STATUS_NEW
from binance.helpers import interval_to_milliseconds, date_to_milliseconds

from TypingClient import TypingClient
from delegate_attribut import custom_inherit
from download_history import download_historical_values
from tools import split_symbol, str_d, Order, Order_attr

log = logging.getLogger(__name__)

def to_str_date(timestamp:int) -> str:
    return datetime.utcfromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')

def to_datetime(binance_datetime: str):
    return datetime.fromtimestamp(int(binance_datetime) / 1000)


class SimulateFromHistory():
    GARDE_PERIOD=10
    def __init__(self, symbol: str):  # TODO: debut et fin de simulation
        self.symbol = symbol
        self._datas = download_historical_values(symbol, "1d")
        self._now = self._datas.iloc[SimulateFromHistory.GARDE_PERIOD]['unix'] * 1000  # En millisecondes (et non en secondes)

    def add_order(self, ts: int, val: Decimal, vol: Decimal):
        pass

    def generate_values(self) -> Generator[Decimal, Any, Any]:
        for _, row in self._datas[SimulateFromHistory.GARDE_PERIOD:].iterrows():
            self._now = row['unix'] * 1000
            yield Decimal(row['open'])
            self._now = row['unix'] * 1000 + 1
            # High et low dans un ordre aléatoire, mais répétable
            yield Decimal(row['high']) if int(row['open']) % 2 == 0 else Decimal(row['low'])
            self._now = row['unix'] * 1000 + 2
            yield Decimal(row['low']) if int(row['open']) % 2 == 0 else Decimal(row['high'])
            self._now = row['unix'] * 1000 + 3
            yield Decimal(row['close'])

    @property
    def now(self):
        return self._now

    #         :param symbol: required
    #         :type symbol: str
    #         :param interval: -
    #         :type interval: str
    #         :param limit: - Default 500; max 500.
    #         :type limit: int
    #         :param startTime:
    #         :type startTime: int
    #         :param endTime:
    #         :type endTime: int
    def get_klines(self,
                   symbol:str,
                   interval:str,
                   startTime:Optional[int]=None,
                   endTime:Optional[int]=None,
                   limit:int=500) -> List[List[Any]]:
        return self.get_historical_klines(interval=interval,
                                   start_str=to_str_date(startTime) if startTime else "now",
                                   end_str=to_str_date(endTime) if endTime else None,
                                   limit=limit)

    def get_historical_klines(self,
                              interval: str,
                              start_str: str,
                              end_str: Optional[str] = None,
                              limit: int = 500,
                              klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT) -> List[List[Any]]:
        """ Recalcul les klines à parti d'un historique. """
        now = date_to_milliseconds("now UTC")
        delta_now = now - self._now

        history = date_to_milliseconds(start_str) - delta_now

        interval_milli = interval_to_milliseconds(interval)
        assert interval_milli, f"Interval non valide ({interval})"

        result = [[row["unix"],
                   Decimal(row["open"]),
                   Decimal(row["high"]),
                   Decimal(row["low"]),
                   Decimal(row["close"]),
                   row["unix"], 1, 1, 1, 1, ""]
                  for _, row
                  in self._datas[self._datas["unix"].between(history/1000, (history + interval_milli)/1000)].iterrows()]
        if len(result) > limit:
            result = result[:limit]
        # TODO: conversion d'echelle > uniquement ?
        # open_time = history
        # while True:
        #     low = sys.float_info.max
        #     high = Decimal(0)
        #     open = Decimal(0)
        #     number_of_trade = 0
        #     taker_buy_base_asset_volume = 0
        #     first = False
        #     kl_close = Decimal(0)
        #     for _,(ts, _, _, kl_open, kl_high, kl_low, kl_close, kl_vol_base,kl_vol_quote,_) in \
        #             self._datas[self._datas["unix"].between(history, (history + interval))].iterrows():
        #         if not first:
        #             open = kl_open
        #             first = True
        #
        #         low = min(low, kl_low)
        #         high = max(high, kl_high)
        #         number_of_trade += 1
        #         taker_buy_base_asset_volume += 1  # FIXME vol
        #     close = kl_close
        #     close_time = open_time  # FIXME: ce n'est pas correct
        #     quote_asset_volume = "0"  # FIXME
        #     taker_buy_quote_asset_volume = "0"  # FIXME
        #     result.append([open_time, open, high, low, close, close_time,
        #                    quote_asset_volume, number_of_trade,
        #                    taker_buy_base_asset_volume, taker_buy_quote_asset_volume, "Ignore"])
        #     open_time += interval
        #     if open_time > now or len(result) > limit:
        #         break
        return result


class SimulateRandomValues():
    """ Simulation via un génerator aléatoire dans un range. """

    def __init__(self, socket_manager: 'SimulateBinanceSocketManager',
                 symbol: str,
                 min: int = 30000,
                 max: int = 50000,
                 step: int = 1000):
        self._socket_manager = socket_manager
        self.symbol = symbol
        self._min = min
        self._max = max
        self._step = step
        history = date_to_milliseconds("1 day ago UTC")
        interval = interval_to_milliseconds("30m")
        now = date_to_milliseconds("now")
        iterator = self.generate_values()
        self._is_started = False
        self._history = [(dt, next(iterator), Decimal(1)) for dt in range(history, now, interval)]
        self._is_started = True

    @property
    def now(self):
        return date_to_milliseconds("now")

    def add_order(self, ts: int, val: Decimal, vol: Decimal):
        self._history.append([ts, val, vol])

    def _gen_value(self) -> Decimal:
        if self._is_started:
            self._socket_manager.add_multicast_event({
                # FIXME
            })
        return Decimal(random.randrange(self._min, self._max, self._step))

    def generate_values(self) -> Generator[Decimal, Any, Any]:
        # Generateur sans fin
        return (self._gen_value() for i in iter(int, 1))

    # [
    #     1499040000000,      // Open time
    #     "0.01634790",       // Open
    #     "0.80000000",       // High
    #     "0.01575800",       // Low
    #     "0.01577100",       // Close
    #     "148976.11427815",  // Volume
    #     1499644799999,      // Close time
    #     "2434.19055334",    // Quote asset volume
    #     308,                // Number of trades
    #     "1756.87402397",    // Taker buy base asset volume
    #     "28.46694368",      // Taker buy quote asset volume
    #     "17928899.62484339" // Ignore.
    #   ]
    def get_historical_klines(self,
                              interval: str,
                              start_str: str,
                              end_str: Optional[str] = None,
                              limit: int = 500,
                              klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT):
        """ Recalcul les klines à parti d'un historique. """
        history = date_to_milliseconds(start_str)
        interval = interval_to_milliseconds(interval)
        now = date_to_milliseconds("now")

        open_time = history

        result = []

        while True:
            low = sys.float_info.max
            high = Decimal(0)
            open = Decimal(0)
            close = Decimal(0)
            number_of_trade = 0
            taker_buy_base_asset_volume = 0
            first = False
            for ts, val, vol in filter(lambda x: open_time <= x[0] < open_time + interval, self._history):
                if not first:
                    open = val
                    first = True
                low = min(low, val)
                high = max(high, val)
                number_of_trade += 1
                taker_buy_base_asset_volume += vol
            close = val
            close_time = open_time  # FIXME: ce n'est pas correct
            quote_asset_volume = "0"  # FIXME
            taker_buy_quote_asset_volume = "0"  # FIXME
            result.append([open_time, open, high, low, close, close_time,
                           quote_asset_volume, number_of_trade,
                           taker_buy_base_asset_volume, taker_buy_quote_asset_volume, "Ignore"])
            open_time += interval
            if open_time > now or len(result) > limit:
                break
        return result


# def generate_values() -> Iterator:
#     return iter([36000, 37000, 38000, 39000, 40000, 41000, 42000, 43000])


# Une delegation du client Binance, pour convertir - à la demande - les str en Decimal
# et les dates en Date.
# Cette classe va évoluer au fur et à mesure des besoins.
class SimulateClient(TypingClient):
    log_current=0
    @classmethod
    async def create(
            cls,
            api_key: Optional[str] = None,
            api_secret: Optional[str] = None,
            **kw
    ):
        return SimulateClient(await AsyncClient.create(api_key, api_secret, **kw))

    def __init__(self, delegate: AsyncClient):
        super().__init__(delegate)
        self._orders = []
        self._order_id = 0
        self._account = {
            "makerCommission": 0,
            "takerCommission": 0,
            "buyerCommission": 0,
            "sellerCommission": 0,
            "canTrade": True,
            "canWithdraw": False,
            "canDeposit": False,
            "updateTime": 0,
            "accountType": "SPOT",
            "balances": [
                {"asset": "BNB", "free": Decimal("1000"), "locked": Decimal(0)},
                {"asset": "BTC", "free": Decimal("1"), "locked": Decimal(0)},
                {"asset": "BUSD", "free": Decimal("1000"), "locked": Decimal(0)},
                {"asset": "ETH", "free": Decimal("1000"), "locked": Decimal(0)},
                {"asset": "LTC", "free": Decimal("0.1"), "locked": Decimal(0)},
                {"asset": "TRX", "free": Decimal("1000"), "locked": Decimal(0)},
                {"asset": "USDT", "free": Decimal("1000"), "locked": Decimal(0)},
                {"asset": "XRP", "free": Decimal("1000"), "locked": Decimal(0)},
            ],
            "permissions": ["SPOT"]
        }
        self._socket_manager = SimulateBinanceSocketManager(self._delegate)
        # FIXME self._simulate_values = SimulateRandomValues(self._socket_manager)
        self._simulate_values = SimulateFromHistory(symbol="BTCUSDT")
        self._values = self._simulate_values.generate_values()

    def _split_balance(self, symbol) -> Tuple[Dict[str, Order_attr], Dict[str, Order_attr]]:
        """ Récupère les bases des 2 devices """
        base, quote = split_symbol(symbol)
        balance_base = next(filter(lambda x: x['asset'] == base, self._account['balances']))
        balance_quote = next(filter(lambda x: x['asset'] == quote, self._account['balances']))
        return balance_base, balance_quote

    def apply_order(self, current_price: Decimal, order: Order):
        balance_base, balance_quote = self._split_balance(order["symbol"])
        quantity = Decimal(order["quantity"])

        if order["type"] == ORDER_TYPE_MARKET:
            order["price"] = current_price
            order["cummulativeQuoteQty"] = str_d(quantity)
            order["executedQty"] = str_d(quantity)
            order["status"] = ORDER_STATUS_FILLED
            del order["newClientOrderId"]
            self._simulate_values.add_order(order["transactTime"], current_price, quantity)
            self._socket_manager.add_user_socket_event({
                'e': "executionReport",
                's': order["symbol"],
                'i': order["orderId"],
                'X': order["status"]
            })

        elif order["type"] == ORDER_TYPE_LIMIT:
            # TODO: timeInForce
            limit = Decimal(order["price"])
            if order["side"] == SIDE_SELL and current_price >= limit:
                order["price"] = current_price
                order["cummulativeQuoteQty"] = str_d(quantity)
                order["executedQty"] = str_d(quantity)
                order["status"] = ORDER_STATUS_FILLED
                balance_base["locked"] -= quantity
                # Plus nécessaire, c'est vendu balance_base["free"] += quantity
                balance_quote["free"] += quantity * current_price
                self._socket_manager.add_user_socket_event({
                    'e': "executionReport",
                    's': order["symbol"],
                    'i': order["orderId"],
                    'X': order["status"]
                })
                self._simulate_values.add_order(order["transactTime"], current_price, quantity)
                # del order["newClientOrderId"]
            elif order["side"] == SIDE_BUY and current_price <= limit:
                order["price"] = current_price
                order["cummulativeQuoteQty"] = str_d(quantity)
                order["executedQty"] = str_d(quantity)
                order["status"] = ORDER_STATUS_FILLED
                balance_quote["locked"] -= quantity * order["price"]
                balance_quote["free"] += quantity * order["price"]
                balance_quote["free"] -= quantity * current_price
                balance_base["free"] += quantity
                # del order["newClientOrderId"]
                self._socket_manager.add_user_socket_event({  # TODO: vérifier la capture du message, puis simplifier
                    'e': "executionReport",
                    's': order["symbol"],
                    'i': order["orderId"],
                    'X': order["status"]
                })
                self._simulate_values.add_order(order["transactTime"], current_price, quantity)  # FIXME: + ou - ?

        else:
            assert False, "Order type not managed"

    def _adjuste_balance(self, balance_base, balance_quote, order: Order, quantity: Decimal):
        if order["side"] == SIDE_SELL:
            balance_base["free"] += quantity
            balance_base["locked"] -= quantity
        else:
            balance_quote["free"] += quantity
            balance_quote["locked"] -= quantity

    def add_order(self, order: Order) -> Order:
        log.debug(f"Add order {order}")
        balance_base, balance_quote = self._split_balance(order["symbol"])
        quantity = Decimal(order["quantity"])
        if order["type"] in (ORDER_TYPE_LIMIT):
            if order["side"] == SIDE_SELL:
                balance_base["free"] -= quantity  # FIXME
                balance_base["locked"] += quantity
            else:
                limit = Decimal(order["price"])
                balance_quote["free"] -= quantity * limit
                balance_quote["locked"] += quantity * limit
        self._orders.append(order)
        return order

    async def tick(self):
        """ Avance la simulation d'une valeur """

        # Avance d'une valeur
        current_value = next(self._values)
        SimulateClient.log_current += 1
        if SimulateClient.log_current % 100 == 0:
            str_now = to_str_date(self._simulate_values.now)
            log.info(f"{str_now} current={current_value}")

        # Analyse des ordres à traiter
        for order in filter(lambda order: order["status"] in (ORDER_STATUS_NEW), self._orders):
            if order["type"] == ORDER_TYPE_MARKET:
                self.apply_order(current_value, order)
            elif order["type"] == ORDER_TYPE_LIMIT:
                self.apply_order(current_value, order)

    async def get_recent_trades(self, **kwargs) -> List[Dict[str, Any]]:
        result = await super().get_recent_trades(**kwargs)
        return result

    async def get_aggregate_trades(self, **kwargs) -> List[Dict[str, Any]]:
        result = await super().get_aggregate_trades(**kwargs)
        return result

    async def get_klines(self, **params) -> Dict:
        result = await super().get_klines(**params)
        return result

    async def get_historical_klines(self, symbol, interval, start_str, end_str=None, limit=500,
                                    klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT):
        result = await super().get_historical_klines(symbol, interval, start_str, end_str, limit, klines_type)
        return result

    async def get_historical_klines_generator(self, **kwargs):
        result = await super().get_historical_klines_generator(**kwargs)
        return result

    async def get_account(self, **kwargs):
        return self._account

    async def get_asset_balance(self, **kwargs):
        result = await super().get_asset_balance(**kwargs)
        return result

    async def get_symbol_ticker(self, **params):
        if params["symbol"] == self._simulate_values.symbol:
            result = next(self._values)
            await self.tick()
            return \
                {
                    "symbol": params["symbol"],
                    "price": result
                }
        else:
            return super().get_symbol_ticker(**params)

    async def create_test_order(self, **params):
        """Test new order creation and signature/recvWindow long. Creates and validates a new order but does not send it into the matching engine.

        https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade

        :param symbol: required
        :type symbol: str
        :param side: required
        :type side: str
        :param type: required
        :type type: str
        :param timeInForce: required if limit order
        :type timeInForce: str
        :param quantity: required
        :type quantity: decimal
        :param price: required
        :type price: str
        :param newClientOrderId: A unique id for the order. Automatically generated if not sent.
        :type newClientOrderId: str
        :param icebergQty: Used with iceberg orders
        :type icebergQty: decimal
        :param newOrderRespType: Set the response JSON. ACK, RESULT, or FULL; default: RESULT.
        :type newOrderRespType: str
        :param recvWindow: The number of milliseconds the request is valid for
        :type recvWindow: int

        :returns: API response
        """
        return

    async def create_order(self, **order):
        """Send in a new order

        Any order with an icebergQty MUST have timeInForce set to GTC.

        https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order--trade

        :param symbol: required
        :type symbol: str
        :param side: required
        :type side: str
        :param type: required
        :type type: str
        :param timeInForce: required if limit order
        :type timeInForce: str
        :param quantity: required
        :type quantity: decimal
        :param quoteOrderQty: amount the user wants to spend (when buying) or receive (when selling)
            of the quote asset, applicable to MARKET orders
        :type quoteOrderQty: decimal
        :param price: required
        :type price: str
        :param newClientOrderId: A unique id for the order. Automatically generated if not sent.
        :type newClientOrderId: str
        :param icebergQty: Used with LIMIT, STOP_LOSS_LIMIT, and TAKE_PROFIT_LIMIT to create an iceberg order.
        :type icebergQty: decimal
        :param newOrderRespType: Set the response JSON. ACK, RESULT, or FULL; default: RESULT.
        :type newOrderRespType: str
        :param recvWindow: the number of milliseconds the request is valid for
        :type recvWindow: int

        :returns: API response

        Response ACK:
    """

        order["clientOrderId"] = order['newClientOrderId']
        order["origQty"] = order["quantity"]
        order["cummulativeQuoteQty"] = '0'
        order["executedQty"] = '0'
        order["fills"] = []
        self._order_id += 1
        order["orderId"] = self._order_id
        order["orderListId"] = -1
        order["transactTime"] = int(datetime.now().timestamp() * 1000.0)
        order["status"] = ORDER_STATUS_NEW
        new_order = self.add_order(order.copy()).copy()
        await self.tick()
        return new_order

    async def get_all_orders(self, symbol) -> Iterable[Dict[str, Any]]:
        return filter(lambda order: order["symbol"] == symbol, self._orders)

    async def get_order(self, symbol, orderId, **kwargs):
        """Check an order's status. Either orderId or origClientOrderId must be sent.

        https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-user_data

        :param symbol: required
        :type symbol: str
        :param orderId: The unique order id
        :type orderId: int
        :param origClientOrderId: optional
        :type origClientOrderId: str
        :param recvWindow: the number of milliseconds the request is valid for
        :type recvWindow: int

        :returns: API response
        """
        # FIXME: None ou exception si pas là ?
        new_order = next(
            filter(lambda order: order["symbol"] == symbol and order.get("orderId", "") == orderId, self._orders))
        await self.tick()
        return new_order

    async def get_klines(self, **params) -> Dict:
        return self._simulate_values.get_klines(**params)

    async def get_historical_klines(self, symbol, interval, start_str, end_str=None, limit=500,
                                    klines_type: HistoricalKlinesType = HistoricalKlinesType.SPOT):
        return self._simulate_values.get_historical_klines(interval, start_str, end_str, limit, klines_type)


    def getBinanceSocketManager(self):
        return SimulateBinanceSocketManager(self._delegate)


@custom_inherit(AsyncClient, delegator='_delegate')
class SimulateBinanceSocketManager():
    def __init__(self, delegate: BinanceSocketManager):
        self._delegate = delegate
        self._queue_user = Queue()
        self._queue_multiplex = Queue()

    def user_socket(self):
        return SimulateUserSocket(self._queue_user)

    def multiplex_socket(self, socket: Iterable[str]):
        return SimulateMultiplexSocket(self._queue_multiplex)

    def add_user_socket_event(self, event: Dict[str, Any]):
        self._queue_user.put_nowait(event)

    def add_multicast_event(self, event: Dict[str, Any]):
        self._queue_multiplex.put_nowait(event)


class SimulateUserSocket():
    def __init__(self, queue: Queue):
        self._queue = queue

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self

    async def recv(self):
        return await self._queue.get()


class SimulateMultiplexSocket():
    def __init__(self, queue: Queue):
        self._queue = queue

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self

    async def recv(self):
        return await self._queue.get()


class SimulationUserStream():
    pass
