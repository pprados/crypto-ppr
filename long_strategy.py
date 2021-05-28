import asyncio
import json
import logging
from asyncio import sleep
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Any

import jstyleson as json
from binance import AsyncClient, BinanceSocketManager
from binance.enums import SIDE_BUY, ORDER_STATUS_FILLED, ORDER_STATUS_NEW, \
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC


# Mémorise l'état de l'automate, pour permettre une reprise à froid
from multiplex_stream import add_multiplex_socket
from user_stream import add_user_socket


class State(dict):
    def __init__(self, *args, **kwargs):
        super(State, self).__init__(*args, **kwargs)
        self.__dict__ = self

async def agent(client: AsyncClient, name: str, conf: Dict[str, Any]) -> None:
    # Récupération du symbol
    symbol = conf["symbol"]
    # Must be called only at the beginning

    path = Path("ctx",name + ".json")
    user_queue = asyncio.Queue()
    market_queue = asyncio.Queue()

    def save_state():
        # with open(path, "w") as f:
        #     json.dump(ctx, f)
        pass # FIXME
    # Conf par défaut
    ctx = State({"state": "init"})

    # Lecture de l'état courant de l'agent
    if path.exists():
        try:
            with open(path) as f:
                ctx = State(json.load(f))
                logging.info(f"{name}{conf} re-started from snapshot")
        except JSONDecodeError:
            logging.error(f"Json decode error for {name}. Must resynchronise with API")
            # TODO: reset depuis le market
    else:
        logging.info(f"{name}{conf} started")

    # Peux recevoir des messages non demandés
    # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
    # async def event(msg: Dict[str, Any], ctx: Any) -> None:
    #     market_queue.put_nowait(msg)
    # add_multiplex_socket(symbol.lower()+"@ticker", event)

    # Ajout une queue pour attendre les évenements de l'utilisateur en websocket
    async def user_event(msg: Dict[str, Any]) -> None:
        user_queue.put_nowait(msg)
    add_user_socket(user_event)

    # Clean all new order for symbol
    for order in await client.get_all_orders(symbol=symbol):
        if order["status"] == ORDER_STATUS_NEW:
            await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

    # List open order
    # open_orders=await client.get_open_orders(symbol='BNBBTC')
    # for order in open_orders:
    #     logging.info(f'{name}{conf}: Open order {order["orderId"]}')

    await sleep(2) # Attend le démarrage de user_stream
    info = await client.get_account()
    for b in info['balances']:
        print(f'{b["asset"]}:{b["free"]}/{b["locked"]}')

    # Resynchronise l'état sauvegardé
    if ctx.state == "wait_queue":
        # Si démarre, utilise un pool la première fois
        ctx.state = "get_order_status"

    # Finite state machine
    while True:
        if ctx.state == "init":
            current = (await client.get_symbol_ticker(symbol=symbol))["price"]
            quantity = 0.1
            price = current
            order = await client.create_order(
                symbol=symbol,
                side=SIDE_BUY,
                quantity=quantity,
                type=ORDER_TYPE_MARKET,
                # type=ORDER_TYPE_LIMIT,
                # timeInForce=TIME_IN_FORCE_GTC,
                # price=price
            )
            logging.info(f'{name}{conf}: order {order["orderId"]} created')
            ctx.last_order = order
            #ctx.state = "wait_top"
            ctx.state = "wait_queue"
            save_state()
        elif ctx.state == "wait_queue":
            # Attend en websocket
            msg = await user_queue.get()
            # See https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md
            if msg['e']=="executionReport" and \
                    msg['s'] == symbol and \
                    msg['i'] == ctx['last_order']['orderId'] and \
                    msg['X'] != ORDER_STATUS_NEW:
                ctx.state = "get_order_status"
                logging.info("Receive event for order")
            user_queue.task_done()
        elif ctx.state == "get_order_status":
            # Polling puis attend en websocket
            order = await client.get_order(symbol=ctx['last_order']['symbol'],
                                           orderId=ctx['last_order']['orderId'])
            if order['status'] == ORDER_STATUS_FILLED:
                logging.info(f'{name}{conf}: order {order["orderId"]} filled')
                ctx.state = "wait_bottom"
                save_state()
            elif order['status'] != ORDER_STATUS_NEW:
                logging.error(f'{name}{conf}: order {order["orderId"]} filled')
                ctx.state = "error"
                save_state()
            else:
                ctx.state = "wait_queue"
        else:
            logging.error(f'{name}{conf}: Unknown state \'{ctx["state"]}\'')
            ctx.state = "init"
            save_state()
            return
