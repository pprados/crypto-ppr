import logging
import os
from asyncio import sleep
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from events_queues import EventQueues
from simulate_client import SimulateClient, AbstractSimulateValue, _SimulateUserSocket
from smart_trade import SmartTrade

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = True

pytestmark = pytest.mark.asyncio


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


async def init_test(conf, values):
    class FixedValues(AbstractSimulateValue):
        def __init__(self, values):
            self.symbol = "BTCUSDT"
            self.values = values

        def generate_values(self):
            return (v for v in self.values)

    client = await SimulateClient.create(api_key, api_secret, testnet=test_net,
                                         values=FixedValues(values))
    client_account = await client.get_account()
    bot_name = "Test-Smart-Order"
    event_queues = EventQueues(client)
    event_queues.add_streams(
        [
            # "btcusdt@aggTrade",
            "btcusdt@trade",
            "btcusdt@bookTicker"
        ])
    agent_queue = event_queues.add_queue(bot_name)
    return agent_queue, bot_name, client, client_account, conf, event_queues


async def test_simple_order_with_event():
    """ Test le passage d'un ordre simple, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),     # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          event_queues,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await smart_trade.next()  # Init bot
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await smart_trade.next()  # STATE_WAIT_ADD_ORDER_FILLED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await smart_trade.next()  # STATE_BUY_ORDER_FILLED
    assert smart_trade.is_finished()


@patch.object(_SimulateUserSocket, 'recv')
async def test_simple_order_with_with_pool(mock_recv_method):
    """ Test le passage d'un ordre simple, sans trailing, validé par poll """

    async def mock_recv():
        sleep(10)
        raise ValueError()

    mock_recv_method.side_effect = mock_recv

    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),     # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          event_queues,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await smart_trade.next()  # Init bot
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await smart_trade.next()  # STATE_WAIT_ADD_ORDER_FILLED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await smart_trade.next()  # STATE_BUY_ORDER_FILLED
    assert smart_trade.is_finished()


async def test_positive_trailing_buy_order_from_market():
    """ Test le passage d'un ordre simple, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
        "training": "10.0%"
    }
    values = \
        [
            Decimal(0),  # 1. Init
            Decimal(1000),  # 2. STATE_CREATE_BUY_ORDER, get_symbol_ticket()
            Decimal(0),     # Ignore
            Decimal(901),  # STATE_ADD_ORDER, create_order()
            Decimal(1002),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    sim_socket_manager = client.get_socket_manager()

    sim_socket_manager.add_multicast_events(
        [
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "1100"  # 3. Monte, mais pas trop
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 4. Descent, ajuste le top
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "800"  # 5. Descent, ajuste le top
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 6. Buy
                }
            }])

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          event_queues,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await smart_trade.next()  # Init bot
    assert smart_trade.state == SmartTrade.STATE_TRAILING_BUY
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.activate_trailing_buy
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await smart_trade.next()  # STATE_WAIT_ADD_ORDER_FILLED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    assert smart_trade.buy_order.order['price'] == Decimal(901)
    await smart_trade.next()  # STATE_BUY_ORDER_FILLED
    assert smart_trade.is_finished()


async def test_negative_trailing_buy_order_from_market():
    """ Test le passage d'un ordre simple, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
        "training": "-10.0%"
    }
    values = \
        [
            Decimal(0),     # 1. Init
            Decimal(1000),  # 2. STATE_CREATE_BUY_ORDER, get_symbol_ticket()
            Decimal(0),     # Ignore
            Decimal(901),   # STATE_ADD_ORDER, create_order()
            Decimal(1002),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    sim_socket_manager = client.get_socket_manager()

    sim_socket_manager.add_multicast_events(
        [
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "1000"  # 3. Monte, mais pas trop
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 4. Descent, ajuste le top
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "800"  # 5. Descent, ajuste le top
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 6. Buy
                }
            }])

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          event_queues,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await smart_trade.next()  # Init bot
    assert smart_trade.state == SmartTrade.STATE_TRAILING_BUY
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.activate_trailing_buy
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await smart_trade.next()  # STATE_INIT
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await smart_trade.next()  # STATE_WAIT_ADD_ORDER_FILLED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    assert smart_trade.buy_order.order['price'] == Decimal(901)
    await smart_trade.next()  # STATE_BUY_ORDER_FILLED
    assert smart_trade.is_finished()
