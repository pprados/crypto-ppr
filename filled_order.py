"""
Génerateur en charge de la création d'un ordre et de son application.
Le générateur retour le context qu'il faut sauver pour lui dans l'agent.
L'état order_ctx.state fini par etre STATE_ERROR ou STATE_ORDER_FILLED.
"""
import logging
from asyncio import sleep, Queue
from typing import AsyncGenerator, Tuple, Dict, Any

from binance.enums import ORDER_STATUS_FILLED, ORDER_STATUS_NEW

# Mémorise l'état de l'automate, pour permettre une reprise à froid

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
STATE_ERROR = "order_error"


class Order_Ctx(dict):
    def __init__(self, *args, **kwargs):
        super(Order_Ctx, self).__init__(*args, **kwargs)
        self.__dict__ = self


# FIXME: user_queue ne peux pas être partagés avec plusieurs orders
async def create_order_generator(client, agent_name: str, user_queue: Queue, pre_order) -> \
        Tuple[AsyncGenerator[Dict[str, Any], None], Dict[str, Any]]:
    ctx = Order_Ctx(
        {
            "state": STATE_INIT,
            "pre_order": pre_order
        })
    current_order = order_generator(client, agent_name + "-order", user_queue, ctx)
    ctx = await current_order.asend(None)
    return current_order, ctx


async def restart_order_generator(client, agent_name: str, user_queue: Queue, context):
    current_order = order_generator(client, agent_name + "-order", user_queue, Order_Ctx(context))
    await current_order.asend(None)
    return current_order


async def order_generator(client, agent_name: str, user_queue: Queue, ctx):
    # Resynchronise l'état sauvegardé
    if ctx.state in (STATE_ORDER_CONFIRMED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
        # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
        # Donc, la première fois, on doit utiliser le pooling
        ctx.state = STATE_WAIT_ORDER_FILLED_WITH_POLLING
    yield ctx
    symbol = ctx.pre_order['symbol']

    if ctx.state in (STATE_ORDER_CONFIRMED, STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
        # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
        # Donc, la première fois, on doit utiliser le pooling
        ctx.state = STATE_WAIT_ORDER_FILLED_WITH_POLLING

    # Finite state machine
    while True:
        if ctx.state == STATE_INIT:
            ctx.state = STATE_ADD_ORDER
        elif ctx.state == STATE_ADD_ORDER:
            # Prépare la création d'un ordre
            ctx.state = STATE_WAIT_ORDER
            yield ctx

            # Puis essaye de l'executer
            order = await client.create_order(**ctx.pre_order)

            # C'est bon, il est passé
            logging.info(f'{agent_name}{ctx}: order {order["clientOrderId"]} created')
            ctx.last_order = order
            ctx.state = STATE_ORDER_CONFIRMED
            yield ctx

        elif ctx.state == STATE_WAIT_ORDER:
            # Ordre est passé, mais je n'ai pas de confirmation
            # Donc, je le cherche dans la liste des ordres
            orders = await client.get_all_orders(symbol=symbol)
            pending_order = list(
                filter(lambda x: x.get("newClientOrderId", "") == ctx.pre_order["newClientOrderId"], orders))
            if not pending_order:
                # Finalement, l'ordre n'est pas passé, on le relance
                logging.info(f'{agent_name}{ctx}: Resend order {ctx.pre_order["newClientOrderId"]}...')
                order = await client.create_order(**ctx.pre_order)
                logging.info(f'{agent_name}{ctx}: Order {order["clientOrderId"]} created')
                ctx.last_order = order
            else:
                # Il est passé, donc on reprend de là.
                ctx.last_order = pending_order[0]
                logging.info(f'{agent_name}{ctx}: Recover order {order["clientOrderId"]}')
            ctx.state = STATE_ORDER_CONFIRMED
            yield ctx

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
                yield ctx
            elif msg['e'] == "executionReport" and \
                    msg['s'] == symbol and \
                    msg['i'] == ctx['last_order']['orderId'] and \
                    msg['X'] != ORDER_STATUS_NEW:
                ctx.state = STATE_ORDER_FILLED
                yield ctx
                logging.info("Receive event for order")
            user_queue.task_done()
        elif ctx.state == STATE_WAIT_ORDER_FILLED_WITH_POLLING:
            order = await client.get_order(symbol=ctx['last_order']['symbol'],
                                           orderId=ctx['last_order']['orderId'])
            if order['status'] == ORDER_STATUS_FILLED:
                logging.info(f'{agent_name}{ctx}: order {order["orderId"]} filled')
                ctx.state = STATE_ORDER_FILLED
                yield ctx
            elif order['status'] != ORDER_STATUS_NEW:
                logging.error(f'{agent_name}{ctx}: order {order["orderId"]} in error')
                ctx.state = STATE_ERROR
                yield ctx
            else:
                await sleep(POOLING_SLEEP)
        elif ctx.state == STATE_ORDER_FILLED:
            pass
        elif ctx.state == STATE_ERROR:
            pass  # FIXME
        else:
            logging.error(f'{agent_name}{ctx}: Unknown state \'{ctx["state"]}\'')
            ctx.state = STATE_ERROR
            yield ctx
            return
