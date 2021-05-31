import asyncio
import sys
import traceback
from pathlib import Path
from typing import Dict, Any

from binance import AsyncClient
from binance.enums import SIDE_BUY, ORDER_TYPE_MARKET

# Mémorise l'état de l'automate, pour permettre une reprise à froid
from filled_order import *
from tools import atomic_load_json, atomic_save_json, generate_order_id
from user_stream import add_user_socket

# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60

POOLING_SLEEP = 2

STATE_INIT = "init"
STATE_ADD_ORDER = "add_order"
STATE_WAIT_ORDER_FILLED = "wait_order_filled"
STATE_WAIT_UPPER_PRICE = "wait_upper_price"


class Ctx(dict):
    def __init__(self, *args, **kwargs):
        super(Ctx, self).__init__(*args, **kwargs)
        self.__dict__ = self


async def agent(client: AsyncClient,
                agent_name: str,
                agent_queues: Dict[str, Queue],  # All agent queues
                conf: Dict[str, Any]) -> None:
    # TODO: capture des exceptions globale pour alerte
    try:
        # Récupération du symbol
        symbol = conf["symbol"]
        path = Path("ctx", agent_name + ".json")

        input_queue = agent_queues[agent_name]  # Queue to receive msg for user or other agent
        user_queue = asyncio.Queue()  # Queue to receive event from user account
        market_queue = asyncio.Queue()  # Queue to receive event from market

        def save_state():
            atomic_save_json(ctx, path)

        # Conf par défaut
        ctx = Ctx(
            {
                "state": STATE_INIT,
                "order_state": None
            })

        # Lecture de l'état courant de l'agent
        if path.exists():
            obj, rollback = atomic_load_json(path)
            ctx = Ctx(obj)
            if rollback:  # TODO:
                ctx.previous_state = ctx.state
                ctx.state = "resynchronize"
        else:
            logging.info(f"{agent_name}{conf} started")

        # L'enregistrement des streams ne doit être fait qu'au début du traitement
        # Peux recevoir des messages non demandés
        # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
        # async def event(msg: Dict[str, Any], ctx: Any) -> None:
        #     market_queue.put_nowait(msg)
        # add_multiplex_socket(symbol.lower()+"@ticker", event)

        # Ajout une queue pour attendre les évènements de l'utilisateur en websocket
        add_user_socket(lambda msg: user_queue.put_nowait(msg))

        # Clean all new order for symbol
        # for order in await client.get_all_orders(symbol=symbol):
        #     if order["status"] == ORDER_STATUS_NEW:
        #         await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

        # List open order
        # open_orders=await client.get_open_orders(symbol='BNBBTC')
        # for order in open_orders:
        #     logging.info(f'{name}{conf}: Open order {order["orderId"]}')

        # Attend le démarrage des queues user et multiplex
        user_queue_initilized = False
        multiplex_queue_initilized = False
        while not user_queue_initilized or not multiplex_queue_initilized:
            msg = await input_queue.get()
            if msg["from"] == "user_stream" and msg["msg"]=="initialized":
                user_queue_initilized = True
            if msg["from"] == "multiplex_stream" and msg["msg"]=="initialized":
                multiplex_queue_initilized = True

        # info = await client.get_account()
        # for b in info['balances']:
        #     print(f'{b["asset"]}:{b["free"]}/{b["locked"]}')

        # Reprise du generateur order
        if 'order_ctx' in ctx:
            current_order = await restart_order_generator(client, agent_name + "-order", ctx.order_ctx)

        # Finite state machine
        while True:
            if ctx.state == STATE_INIT:
                ctx.state = STATE_ADD_ORDER
                save_state()
            elif ctx.state == STATE_ADD_ORDER:
                current = (await client.get_symbol_ticker(symbol=symbol))["price"]
                quantity = 0.1
                price = current
                # FIXME current_order = order_generator(client, agent_name+"-order", ctx.order_state)
                current_order,ctx.order_ctx = await create_order_generator(
                    client,
                    agent_name + "-order",
                    user_queue,
                    {
                        "symbol": symbol,
                        "side": SIDE_BUY,
                        "quantity": quantity,
                        "type": ORDER_TYPE_MARKET,
                        "newClientOrderId": generate_order_id(agent_name),
                        # type=ORDER_TYPE_LIMIT,
                        # timeInForce=TIME_IN_FORCE_GTC,
                        # price=price

                    }
                )
                ctx.state = STATE_WAIT_ORDER_FILLED
                save_state()
            elif ctx.state == STATE_WAIT_ORDER_FILLED:
                ctx.order_ctx = await current_order.asend(None)
                if ctx.order_ctx.state == STATE_ORDER_FILLED:
                    del ctx.order_ctx
                    ctx.state = STATE_WAIT_UPPER_PRICE
                    save_state()
                elif ctx.order_ctx.state == STATE_ERROR:
                    # FIXME
                    ctx.state == STATE_ERROR
                    save_state()
            elif ctx.state == STATE_WAIT_UPPER_PRICE:
                pass
                sys.exit(1)
    except Exception as e:
        logging.error(e)
        traceback.print_tb(e.__traceback__)
        sys.exit(-1)  # FIXME
