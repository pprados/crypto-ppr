import asyncio
import logging
from asyncio import sleep, Queue
from pathlib import Path
from typing import Dict, Any

from binance import AsyncClient
from binance.enums import SIDE_BUY, ORDER_STATUS_FILLED, ORDER_STATUS_NEW, \
    ORDER_TYPE_MARKET

# Mémorise l'état de l'automate, pour permettre une reprise à froid
from tools import atomic_load_json, atomic_save_json,generate_order_id
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
STATE_WAIT_ORDER = "wait_order"
STATE_ORDER_CONFIRMED = "order_confirmed"
STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET = "wait_order_filled_with_websocket"
STATE_WAIT_ORDER_FILLED_WITH_POLLING = "wait_order_filled_with_polling"
STATE_ORDER_FILLED = "order_filled"
ORDER_IN_ERROR = "order_error"


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
        ctx = Ctx({"state": STATE_INIT})

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

        # Ajout une queue pour attendre les évenements de l'utilisateur en websocket
        add_user_socket(lambda msg: user_queue.put_nowait(msg))

        # Clean all new order for symbol
        # for order in await client.get_all_orders(symbol=symbol):
        #     if order["status"] == ORDER_STATUS_NEW:
        #         await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

        # List open order
        # open_orders=await client.get_open_orders(symbol='BNBBTC')
        # for order in open_orders:
        #     logging.info(f'{name}{conf}: Open order {order["orderId"]}')

        await sleep(2)  # Attend le démarrage de user_stream
        # info = await client.get_account()
        # for b in info['balances']:
        #     print(f'{b["asset"]}:{b["free"]}/{b["locked"]}')

        # Resynchronise l'état sauvegardé
        if ctx.state in (STATE_ORDER_CONFIRMED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            ctx.state = STATE_WAIT_ORDER_FILLED_WITH_POLLING

        # Finite state machine
        while True:
            if ctx.state == "resynchronize":
                if ctx.previous_state == "init":
                    del ctx.previous_state
                    # 1. read current order
                    orders = await client.get_all_orders(symbol=symbol, limit=1)
                    # 2. detect if an order with the symbol
                    if False:  # orders:
                        ctx.last_order = orders[0]
                        ctx.state = "get_order_status"
                    else:
                        # No trade found. Check if has amount for one of the pair
                        info = await client.get_account()
                        pair = list()
                        total = 0
                        for b in info['balances']:
                            # print(f'{b["asset"]}:{b["free"]}/{b["locked"]}')
                            asset = b["asset"]
                            if asset in symbol:
                                total += b['free']
                                pair.append(b)
                        if total:
                            logging.info("Find asset for pair")
                            if symbol.startswith(pair[0]['asset']) and pair[0]['free']:
                                # Membre de gauche avec des coins...
                                ctx.state = "init"
                            else:
                                # Membre de droite avec des coins...
                                ctx.state = "wait_queue"
                        else:
                            logging.error(f"Not assets for pair {symbol}")
                            ctx.state = "error"
            elif ctx.state == STATE_INIT:
                ctx.state = STATE_ADD_ORDER
            elif ctx.state == STATE_ADD_ORDER:
                # Prépare la création d'un ordre
                current = (await client.get_symbol_ticker(symbol=symbol))["price"]
                quantity = 0.1
                price = current
                ctx.pre_order = {
                    "symbol": symbol,
                    "side": SIDE_BUY,
                    "quantity": quantity,
                    "type": ORDER_TYPE_MARKET,
                    "newClientOrderId": generate_order_id(agent_name),
                    # type=ORDER_TYPE_LIMIT,
                    # timeInForce=TIME_IN_FORCE_GTC,
                    # price=price

                }
                ctx.state = STATE_WAIT_ORDER
                save_state()

                # Puis essaye de l'executer
                order = await client.create_order(**ctx.pre_order)

                # C'est bon, il est passé
                logging.info(f'{agent_name}{conf}: order {order["clientOrderId"]} created')
                ctx.last_order = order
                ctx.state = STATE_ORDER_CONFIRMED
                save_state()

            elif ctx.state == STATE_WAIT_ORDER:
                # Ordre est passé, mais je n'ai pas de confirmation
                # Donc, je le cherche dans la liste des ordres
                orders = await client.get_all_orders(symbol=symbol)
                pending_order = list(
                    filter(lambda x: x.get("newClientOrderId", "") == ctx.pre_order["newClientOrderId"], orders))
                if not pending_order:
                    # Finalement, l'order non pas passé, on le refait
                    logging.info(f'{agent_name}{conf}: Resend order {ctx.pre_order["newClientOrderId"]}...')
                    order = await client.create_order(**ctx.pre_order)
                    logging.info(f'{agent_name}{conf}: Order {order["clientOrderId"]} created')
                    del ctx.pre_order
                    ctx.last_order = order
                else:
                    # Il est passé, donc on reprend de là.
                    ctx.last_order = pending_order[0]
                    logging.info(f'{agent_name}{conf}: Recover order {order["clientOrderId"]}')
                ctx.state = STATE_ORDER_CONFIRMED
                save_state()

            elif ctx.state == STATE_ORDER_CONFIRMED:
                # Maintenant, il faut attendre son execution effective
                ctx.state = STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET

            elif ctx.state == STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET:
                # Attend en websocket
                msg = await user_queue.get()
                # See https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md
                if msg['e'] == "error":
                    # Web socket in error
                    ctx.state = STATE_WAIT_ORDER_FILLED_WITH_POLLING
                    save_state()
                elif msg['e'] == "executionReport" and \
                        msg['s'] == symbol and \
                        msg['i'] == ctx['last_order']['orderId'] and \
                        msg['X'] != ORDER_STATUS_NEW:
                    ctx.state = STATE_ORDER_FILLED
                    logging.info("Receive event for order")
                user_queue.task_done()
            elif ctx.state == STATE_WAIT_ORDER_FILLED_WITH_POLLING:
                order = await client.get_order(symbol=ctx['last_order']['symbol'],
                                               orderId=ctx['last_order']['orderId'])
                if order['status'] == ORDER_STATUS_FILLED:
                    logging.info(f'{agent_name}{conf}: order {order["orderId"]} filled')
                    ctx.state = STATE_ORDER_FILLED
                    save_state()
                elif order['status'] != ORDER_STATUS_NEW:
                    logging.error(f'{agent_name}{conf}: order {order["orderId"]} in error')
                    ctx.state = ORDER_IN_ERROR
                    save_state()
                else:
                    await sleep(POOLING_SLEEP)
            elif ctx.state == STATE_ORDER_FILLED:
                pass
            elif ctx.state == ORDER_IN_ERROR:
                pass  # FIXME
            else:
                logging.error(f'{agent_name}{conf}: Unknown state \'{ctx["state"]}\'')
                ctx.state = "init"
                save_state()
                return
    except Exception as e:
        logging.error(e)
