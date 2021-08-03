import logging
import os
from asyncio import sleep
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from binance.enums import ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET, ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT

import global_flags
from api_key import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TEST_NET
from auto_trading import engine_path
from bots.add_order import AddOrder
from bots.smart_trade import SmartTrade
from bots.smart_trades_conf import COND_MARKET_ORDER, COND_LIMIT_ORDER
from conf import NO_TELEGRAM
from engine import Engine
from events_queues import EventQueues
from simulate_client import TestBinanceClient, AbstractSimulateValue, _SimulateUserSocket
from tools import anext

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = True

pytestmark = pytest.mark.asyncio


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture(autouse=True)
def run_before_and_after_tests(tmpdir):
    """Fixture to execute asserts before and after a test is run"""
    # Setup: fill with any logic you want
    assert NO_TELEGRAM, "Set NO_TELEGRAM=True"
    yield  # this is where the testing happens
    # Teardown : fill with any logic you want


async def init_test(conf, values):
    class FixedValues(AbstractSimulateValue):
        def __init__(self, values):
            self.symbol = "BTCUSDT"
            self.values = values

        def generate_values(self):
            return (v for v in self.values)

    engine = Engine(BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TEST_NET, global_flags.simulate, path=engine_path)
    await engine.init()

    client = await TestBinanceClient.create(api_key, api_secret, testnet=test_net,
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
    return engine, agent_queue, bot_name, client, client_account, conf, event_queues


async def test_simple_order_with_unit_at_market_with_event():
    """ Test le passage d'un ordre simple, sur unit, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            # Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            # Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")


async def test_simple_order_with_total_at_market_with_event():
    """ Test le passage d'un ordre simple, sur total, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "total": 110,
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert Decimal(smart_trade.buy_order.order['executedQty']) == Decimal('0.1')


async def test_simple_order_with_size_at_market_with_event():
    """ Test le passage d'un ordre simple, sur size, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "size": "10%",
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert Decimal(smart_trade.buy_order.order['executedQty']) == Decimal('0.1')


@patch.object(_SimulateUserSocket, 'recv')
async def test_simple_order_with_with_poll(mock_recv_method):
    """ Test le passage d'un ordre simple, sans trailing, validé par poll """

    async def mock_recv():
        sleep(10)
        raise ValueError()

    mock_recv_method.side_effect = mock_recv

    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET


async def test_simple_order_with_unit_at_limit_with_event():
    """ Test le passage d'un ordre simple avec limit """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "LIMIT",  # FIXME: "COND_LIMIT_ORDER", "COND_MARKET_ORDER"
        "price": "900"
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(900),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_LIMIT
    assert smart_trade.buy_order.order['price'] == Decimal(900)
    assert smart_trade.buy_order.order['executedQty'] == "0.1"


@pytest.mark.skip(reason="COND_LIMIT_ORDER not implemented")
async def test_simple_order_with_cond_limit_order_at_market_with_event():
    """ Test le passage d'un ordre simple, sur unit, avec condition limit, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "COND_LIMIT_ORDER",
        "price": 1000,
        "order_price": 1001,
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == 0.1


@pytest.mark.skip(reason="COND_MARKET_ORDER not implemented")
async def test_simple_order_with_cond_market_order_at_market_with_event():
    """ Test le passage d'un ordre simple, sur unit, avec condition market, sans trailing, validé par un event """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "COND_MARKET_ORDER",
        "price": 1001,
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == 0.1


async def test_cond_market_simple_order():
    """ Test le passage d'un ordre market en cond limit """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": COND_MARKET_ORDER,
        "cond_price": 1000
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(800),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    sim_socket_manager = client.get_socket_manager()

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_COND_MARKET_ORDER
    sim_socket_manager.add_multicast_events(
        [
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 6. Buy
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "1000"  # 6. Buy
                }
            },
        ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")


async def test_cond_limit_simple_order():
    """ Test le passage d'un ordre market en cond limit """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": COND_LIMIT_ORDER,
        "cond_price": 1000,
        "price": 1100,
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(800),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1100),  # STATE_ADD_ORDER, create_order()
            Decimal(1100),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    sim_socket_manager = client.get_socket_manager()

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_COND_LIMIT_ORDER
    sim_socket_manager.add_multicast_events(
        [
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "900"  # 6. Buy
                }
            },
            {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "p": "1000"  # 6. Buy
                }
            },
        ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_LIMIT
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")


async def test_positive_trailing_buy_order_from_market():
    """ Test le passage d'un ordre simple, avec trailing positif """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
        "trailing": "10.0%"
    }
    values = \
        [
            Decimal(0),  # 1. Init
            Decimal(1000),  # 2. STATE_CREATE_BUY_ORDER, get_symbol_ticket()
            Decimal(0),  # Ignore
            Decimal(901),  # STATE_ADD_ORDER, create_order()
            Decimal(1002),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
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
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING_BUY
    await anext(smart_trade)
    assert smart_trade.activate_trailing_buy
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    assert smart_trade.buy_order.order['price'] == Decimal(901)
    await anext(smart_trade)
    assert smart_trade.is_finished()


async def test_negative_trailing_buy_order_from_market():
    """ Test le passage d'un ordre simple, avec trailing négatif """
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        # "total": 20,
        "mode": "MARKET",
        "trailing": "-10.0%"
    }
    values = \
        [
            Decimal(0),  # 1. Init
            Decimal(1000),  # 2. STATE_CREATE_BUY_ORDER, get_symbol_ticket()
            Decimal(0),  # Ignore
            Decimal(901),  # STATE_ADD_ORDER, create_order()
            Decimal(1002),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    client.get_socket_manager().add_multicast_events(
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
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING_BUY
    await anext(smart_trade)
    assert smart_trade.activate_trailing_buy
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    assert smart_trade.buy_order.order['price'] == Decimal(901)
    await anext(smart_trade)
    assert smart_trade.is_finished()


# -------------------------- Take profit

async def test_simple_order_at_market_tp_last():
    """ Test le passage d'un ordre simple, avec TP simple sur base (order spécial TP sur Binance) """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "last",  # FIXME: last ou ask ?
            "mode": "MARKET",
            "price": "2%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TP_ALONE
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_TAKE_PROFIT_LIMIT


async def test_simple_order_at_market_tp_ask():
    """ Test le passage d'un ordre simple, avec TP simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1010"  # 4. C'est le moment de lancer le TP
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET


async def test_simple_order_at_market_tp_ask_limit():
    """ Test le passage d'un ordre simple, avec TP simple sur ask, en mode limit """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "mode_sell": "LIMIT",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1010"  # 4. C'est le moment de lancer le TP
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    assert smart_trade.take_profit_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_ADD_ORDER
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_LIMIT


async def test_simple_order_at_market_tp_ask_limit_percent():
    """ Test le passage d'un ordre simple, avec TP simple sur ask, en mode limit """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "mode_sell": "-1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1010"  # 4. C'est le moment de lancer le TP
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.take_profit_order.price == Decimal("999.9")
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_LIMIT


async def test_simple_order_at_market_tp_ask_limit_timeout():
    """ Test le passage d'un ordre simple, avec TP simple sur ask, en mode limit """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "mode_sell": "LIMIT",
            "sell_timeout": 2,
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1010"  # 4. C'est le moment de lancer le TP
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_INIT  # Reprend l'ordre en MARKET
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.take_profit_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_take_profit_condition == Decimal("1010")


async def test_simple_order_at_market_tp_bid():
    """ Test le passage d'un ordre simple, avec TP simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "bid",
            "mode": "MARKET",
            "price": "1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "1000",  # 3. Pas encore
                "a": "1200"
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "1010",  # 4. C'est le moment de lancer le TP
                "a": "1020"
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET


async def test_simple_order_at_market_tp_last_trailing_negatif():
    """ Test le passage d'un ordre simple, avec TP trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "last",  # FIXME: last ou ask ?
            "mode": "MARKET",
            "price": "2%",
            "trailing": "-1%",  # Start à la cible, si positif
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1020"  # Active trailing (vend à 1009.8)
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.0
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Active TP
            }
        },
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_take_profit_condition == Decimal(1020)
    assert smart_trade.active_take_profit_sell == Decimal(1089)
    assert smart_trade.active_take_profit_trailing


async def test_simple_order_at_market_tp_last_trailing_positif():
    """ Test le passage d'un ordre simple, avec TP trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "last",  # FIXME: last ou ask ?
            "mode": "MARKET",
            "price": "2%",
            "trailing": "1%",  # Start à la cible, si positif
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1030.2"  # Active trailing (vend à 1020)
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.0
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Active TP
            }
        },
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_take_profit_condition == Decimal("1030.2")
    assert smart_trade.active_take_profit_sell == Decimal("1089")
    assert smart_trade.active_take_profit_trailing


async def test_simple_order_at_market_tp_last_limit_trailing_negatif():
    """ Test le passage d'un ordre simple, avec TP trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "last",  # FIXME: last ou ask ?
            "mode": "LIMIT",
            "price": "1020",
            "trailing": "-1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1020"  # Active trailing (vend à 1009.8)
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.0
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Active TP
            }
        },
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_take_profit_condition == Decimal(1020)
    assert smart_trade.active_take_profit_sell == Decimal(1089)
    assert smart_trade.active_take_profit_trailing


async def test_simple_order_at_market_tp_last_limit_trailing_positif():
    """ Test le passage d'un ordre simple, avec TP trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "last",  # FIXME: last ou ask ?
            "mode": "LIMIT",
            "price": "1020",
            "trailing": "1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1020"  # Active trailing (vend à 1009.8)
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.0
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "900"  # Active TP
            }
        },
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_take_profit_condition == Decimal("1030.20")
    assert smart_trade.active_take_profit_sell == Decimal(1020)
    assert smart_trade.active_take_profit_trailing


async def test_simple_order_at_market_min_tp_activate():
    """ Test le passage d'un ordre simple, avec TP simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "minimal": "0.5%",
            "timeout": 1,
        }
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1007),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1005"  # 4. Le Min TP s'active
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1006"  # 5. Le Min TP est confirmé
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1006"  # 6. Le Min TP est confirmé
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 7. Le Min TP se déclenche à cause de la baisse
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert not smart_trade.activate_min_tp
    assert smart_trade.min_tp_target == Decimal("1005")
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    assert smart_trade.min_tp_triggered
    assert smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)  # Active MinTP
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.take_profit_order.order['price'] == Decimal("1007")
    assert smart_trade.min_tp_activated


async def test_simple_order_trailing_min_tp_activate():
    """ Test le passage d'un ordre simple, avec TP trailing, et min ajusté """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "trailing": "-0.05%",
            "minimal": "0.5%",
            "timeout": 1,
            "trailing_minimal": True,
        }
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1007),  # Take profit
            Decimal(1000),  #
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1005"  # 4. Le Min TP s'active
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1006"  # 5. Le Min TP est confirmé
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1006"  # 6. Le Min TP est confirmé
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1010"  # 7. Le trailing TP s'ajuste
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1100"  # 8. Achat par TP simple
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1095"  # 9. Puis on achete
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert not smart_trade.activate_min_tp
    assert smart_trade.min_tp_target == Decimal("1005")
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    assert smart_trade.active_take_profit_trailing
    await anext(smart_trade)  # Active MinTP
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert smart_trade.min_tp_target == Decimal("1094.955")  # Ajustement du Min-TP
    assert smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    await anext(smart_trade)  # Active MinTP
    assert smart_trade.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
    assert not smart_trade.min_tp_activated  # Activé, mais pas à cause du Min-TP
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_TP_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # STATE_ADD_ORDER
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)  # FINISH
    assert smart_trade.is_finished()
    assert smart_trade.take_profit_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.take_profit_order.order['price'] == Decimal("1007")
    assert not smart_trade.min_tp_activated


async def test_simple_order_at_market_min_tp_no_activate():
    """ Test le passage d'un ordre simple, avec TP simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "take_profit": {
            "base": "ask",
            "mode": "MARKET",
            "price": "1%",
            "minimal": "0.5%",
            "timeout": 2,
        }
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1010),  # Take profit
            Decimal(1000),
        ]
    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)
    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert not smart_trade.activate_min_tp
    assert smart_trade.min_tp_target == Decimal("1005")
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1005"  # 4. Le Min TP s'active
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1006"  # 5. Le Min TP est confirmé
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 6. Le TP se déclenche à cause de la hausse
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 6. Le TP se déclenche à cause de la hausse
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert not smart_trade.activate_min_tp
    assert smart_trade.min_tp_target == Decimal("1005")
    await anext(smart_trade)
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert smart_trade.activate_min_tp  # Peux-etre...
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert not smart_trade.min_tp_triggered
    assert not smart_trade.min_tp_activated
    assert not smart_trade.activate_min_tp  # Peux-etre...
    assert smart_trade.state == SmartTrade.STATE_TRAILING


# -------------------------- Stop loss

async def test_simple_order_at_market_sl_last():
    """ Test le passage d'un ordre simple, avec SL simple sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "last",
            "mode": "MARKET",
            "price": "-1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_SL_ALONE
    await anext(smart_trade)
    assert smart_trade.buy_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED;
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_STOP_LOSS_LIMIT


async def test_simple_order_at_market_sl_ask():
    """ Test le passage d'un ordre simple, avec SL simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "ask",
            "mode": "MARKET",
            "price": "-1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1100"  # Ajuste
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "990"  # 4. C'est le moment de lancer le SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_stop_loss_condition == Decimal("990.0")


async def test_simple_order_at_market_sl_ask_limit():
    """ Test le passage d'un ordre simple, avec SL simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "ask",
            "mode": "MARKET",
            "price": "-1%",
            "mode_sell": "LIMIT",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1100"  # Ajuste
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "990"  # 4. C'est le moment de lancer le SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_LIMIT
    assert smart_trade.active_stop_loss_condition == Decimal("990.0")


async def test_simple_order_at_market_sl_ask_limit_price():
    """ Test le passage d'un ordre simple, avec SL simple en valeur sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "ask",
            "mode": "MARKET",
            "price": "-20",
            "mode_sell": "LIMIT",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "800"  # Cible
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_LIMIT
    assert smart_trade.active_stop_loss_condition == Decimal("800.0")


async def test_simple_order_at_market_sl_ask_limit_timeout():
    """ Test le passage d'un ordre simple, avec SL simple sur ask """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "ask",
            "mode": "MARKET",
            "price": "-1%",
            "mode_sell": "LIMIT",
            "sell_timeout": 2,
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "1100"  # Ajuste
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "900",
                "a": "990"  # 4. C'est le moment de lancer le SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT  # Reprise au market
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.state == SmartTrade.STATE_FINISH
    await anext(smart_trade)
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_stop_loss_condition == Decimal("990.0")


async def test_simple_order_at_market_sl_bid():
    """ Test le passage d'un ordre simple, avec SL simple sur bid """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "bid",
            "mode": "MARKET",
            "price": "-1%",
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(900),  # Stop loss
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "1010",
                "a": "1000"  # 3. Pas encore
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "1000",  # Ajuste
                "a": "900"
            }
        },
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "b": "990",  # 4. C'est le moment de lancer le SL
                "a": "900"
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_stop_loss_condition == Decimal("990.00")


async def test_simple_order_at_market_sl_last_trailing():
    """ Test le passage d'un ordre simple, avec SL trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "last",
            "mode": "MARKET",
            "price": "-1%",
            "trailing": True,  # Start à la cible, si positif
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.00
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Active SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_stop_loss_condition == Decimal(1089)


async def test_simple_order_at_limit_sl_last_trailing():
    """ Test le passage d'un ordre simple, avec SL trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "last",
            "mode": "MARKET",
            "price": 990,
            "trailing": True,  # Start à la cible, si positif
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.00
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Active SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.active_stop_loss_condition == Decimal(1089)


async def test_simple_order_at_cond_limit_sl_last_trailing():
    """ Test le passage d'un ordre simple, avec SL trailing sur last """
    # Dont générer un order TP
    conf = {
        "symbol": "BTCUSDT",
        "unit": 0.1,
        "mode": "MARKET",
        "stop_loss": {
            "base": "last",
            "mode": "COND_LIMIT_ORDER",
            "price": 990,
            "order_price": 989,
            "trailing": True,  # Start à la cible, si positif
        },
    }
    values = \
        [
            Decimal(0),  # Init
            Decimal(1000),  # STATE_CREATE_BUY_ORDER, get market
            Decimal(1000),  # STATE_ADD_ORDER, create_order()
            Decimal(1000),  # STATE_WAIT_ORDER_FILLED_WITH_POLLING, get_order()
            Decimal(1100),  # Take profit
            Decimal(1000),  #
        ]

    # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
    engine, agent_queue, bot_name, client, client_account, conf, event_queues = await init_test(conf, values)

    # Execution du generator
    json_generator = {}  # Initial state
    log = logging.getLogger("TEST")
    smart_trade = await SmartTrade.create(client,
                                          engine,
                                          agent_queue,
                                          log,
                                          json_generator,
                                          generator_name=bot_name,
                                          client_account=client_account,
                                          conf=conf,
                                          )
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_CREATE_BUY_ORDER
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # INIT
    await anext(smart_trade)  # ADD_ORDER_ACCEPTED
    await anext(smart_trade)  # STATE_WAIT_ADD_ORDER_FILLED
    await anext(smart_trade)  # STATE_ORDER_CONFIRMED
    await anext(smart_trade)  # STATE_WAIT_ORDER_FILLED_WITH_POLLING
    assert smart_trade.state == SmartTrade.STATE_BUY_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    client.get_socket_manager().add_multicast_events([
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Pas encore
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1100"  # Ajuste sell à 1089.00
            }
        },
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "p": "1000"  # Active SL
            }
        }
    ])
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_TRAILING
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_SL
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_INIT
    await anext(smart_trade)
    assert smart_trade.state == SmartTrade.STATE_WAIT_SL_FILLED
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ADD_ORDER_ACCEPTED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_CONFIRMED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    await anext(smart_trade)
    assert smart_trade.stop_loss_order.state == AddOrder.STATE_ORDER_FILLED
    assert smart_trade.is_finished()
    assert smart_trade.buy_order.order['type'] == ORDER_TYPE_MARKET
    assert smart_trade.buy_order.order['quantity'] == Decimal("0.1")
    assert smart_trade.stop_loss_order.order['type'] == ORDER_TYPE_LIMIT
    assert smart_trade.active_stop_loss_condition == Decimal(1089)
