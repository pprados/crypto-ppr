"""
Génerateur en charge de la création d'un ordre et de son application.
Le générateur retour le context qu'il faut sauver pour lui dans l'agent.
L'état order_ctx.state fini par etre STATE_ERROR ou STATE_ORDER_FILLED.
"""
import asyncio
import logging
from asyncio import Queue, wait_for
from decimal import Decimal
from queue import Empty
from typing import Dict, Any

from binance import AsyncClient
from binance.enums import ORDER_STATUS_FILLED, ORDER_STATUS_NEW, ORDER_STATUS_REJECTED, ORDER_STATUS_EXPIRED, \
    ORDER_STATUS_PARTIALLY_FILLED
# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60
from binance.exceptions import BinanceAPIException

from bot_generator import BotGenerator
from conf import STREAM_MSG_TIMEOUT
from tools import log_order, update_wallet


class AddOrder(BotGenerator):
    POOLING_SLEEP = 2

    STATE_INIT = "init"
    STATE_ADD_ORDER = "add_order"
    STATE_WAIT_ORDER = "wait_order"
    STATE_ORDER_CONFIRMED = "order_confirmed"
    STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET = "wait_order_filled_with_websocket"
    STATE_WAIT_ORDER_FILLED_WITH_POLLING = "wait_order_filled_with_polling"
    STATE_ORDER_FILLED = "order_filled"
    STATE_ORDER_EXPIRED = "order_expired"
    STATE_CANCELING = "order_canceling"
    STATE_CANCELED = "order_canceled"
    STATE_ERROR = "order_error"

    async def _start(self,
                     client: AsyncClient,
                     user_queue: Queue,
                     log: logging,
                     init: Dict[str, Any],
                     **kwargs) -> 'AddOrder':
        self._generator = self.generator(client,
                                         user_queue,
                                         log,
                                         init,
                                         **kwargs)
        await self.next()
        return self

    def is_filled(self):
        return self.state == AddOrder.STATE_ORDER_FILLED

    # FIXME
    # def is_partially_filled(self):
    #     return self.state == AddOrder.STATE_ORDER_FILLED

    def is_error(self):
        return self.state == AddOrder.STATE_ERROR

    def is_waiting(self):
        return self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING

    def cancel(self):
        self.previous_state = self.state
        self.state = AddOrder.STATE_CANCELING

    def is_canceled(self):
        return self.state == AddOrder.STATE_CANCELED

    async def generator(self,
                        client,
                        user_queue: Queue,
                        log: logging,
                        init: Dict[str, Any],
                        **kwargs):
        if not init:
            init = {
                "state": AddOrder.STATE_INIT,
                "order": kwargs['order']
            }
        self.update(init)
        wallet:Dict[str,Decimal] = kwargs['wallet']
        self.continue_if_partially:bool = kwargs.get("continue_if_partially")
        # Resynchronise l'état sauvegardé
        if self.state in (AddOrder.STATE_ORDER_CONFIRMED, AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
        yield self
        symbol = self.order['symbol']

        def _get_user_msg():
            return user_queue.get()

        if self.state in (AddOrder.STATE_ORDER_CONFIRMED, AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING

        # Finite state machine
        while True:
            log.debug(f"filled_order=> {self.state}")
            if self.state == AddOrder.STATE_INIT:
                self.state = AddOrder.STATE_ADD_ORDER
            elif self.state == AddOrder.STATE_ADD_ORDER:
                # Prépare la création d'un ordre
                await client.create_test_order(**self.order)
                # Puis essaye de l'executer
                try:
                    order = await client.create_order(**self.order)
                except BinanceAPIException as ex:
                    if ex.code == -2010:  # Duplicate order sent ?, ignore
                        # TODO: il faut analyser le text
                        if ex.message == "Duplicate order sent.":
                            # Retrouve l'ordre dupliqué.
                            orders = await client.get_all_orders(symbol=symbol)
                            order = next(
                                filter(lambda x: x.get("clientOrderId", "") == self.order["newClientOrderId"], orders))
                            # et continue

                        elif ex.message == "Account has insufficient balance for requested action.":
                            self.state = AddOrder.STATE_ERROR
                            yield self
                            continue
                        elif ex.message.startswith("Filter failure:"):
                            raise
                        elif ex.message == "Stop price would trigger immediately.":
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            raise
                    else:
                        raise

                # C'est bon, il est passé
                self.order = order
                log.debug(f'Order \'{self.order["clientOrderId"]}\' created')
                self.state = AddOrder.STATE_ORDER_CONFIRMED
                yield self

            elif self.state == AddOrder.STATE_WAIT_ORDER:
                # Ordre est passé, mais je n'ai pas de confirmation
                # Donc, je le cherche dans la liste des ordres
                orders = await client.get_all_orders(symbol=symbol)
                pending_order = list(
                    filter(lambda x: x.get("newClientOrderId", "") == self.order["newClientOrderId"], orders))
                if not pending_order:
                    # Finalement, l'ordre n'est pas passé, on le relance
                    log.info(f'Resend order {self.order["newClientOrderId"]}...')
                    order = await client.create_order(**self.order)
                    log.debug(f'Order {order["clientOrderId"]} created')
                    self.order = order
                else:
                    # Il est passé, donc on reprend de là.
                    self.order = pending_order[0]
                    log.info(f'Recover order {order["clientOrderId"]}')
                self.state = AddOrder.STATE_ORDER_CONFIRMED
                yield self

            elif self.state == AddOrder.STATE_ORDER_CONFIRMED:
                # Maintenant, il faut attendre son execution effective
                self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET

            elif self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET:
                # Attend en websocket
                try:

                    log.debug("Wait event order status...")
                    # FIXME: incompatible avec plusieurs ordres en meme temps
                    msg = await wait_for(_get_user_msg(), timeout=STREAM_MSG_TIMEOUT)
                    # See https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md
                    if msg['e'] == "error":
                        # Web socket in error
                        self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                        yield self
                    elif msg['e'] == "executionReport" and \
                            msg['s'] == symbol and \
                            msg['i'] == self['order']['orderId'] and \
                            msg['X'] != ORDER_STATUS_NEW:
                        self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                        yield self
                        log.info("Receive event for order")
                    user_queue.task_done()
                except (asyncio.TimeoutError, Empty) as ex:
                    # Periodiquement, essaye en polling
                    self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                    yield self
            elif self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING:
                # log.info("Polling get order status...")
                order = await client.get_order(symbol=self['order']['symbol'],
                                               orderId=self['order']['orderId'])
                # See https://binance-docs.github.io/apidocs/spot/en/#public-api-definitions
                if order['status'] == ORDER_STATUS_FILLED:  # or ORDER_STATUS_IOY_FILLED
                    update_wallet(wallet, order)
                    log_order(log, order)
                    self.order = order
                    self.state = AddOrder.STATE_ORDER_FILLED
                    yield self
                if order['status'] == ORDER_STATUS_PARTIALLY_FILLED:  # or ORDER_STATUS_PARTIALLY_FILLED
                    # FIXME: Partially a traiter
                    log_order(log,order)
                    update_wallet(wallet, order)
                    log_order(log, order)
                    if not self.continue_if_partially:
                        log.warning("PARTIALLY")
                        self.order = order
                        self.state = AddOrder.STATE_ORDER_FILLED
                    else:
                        # FIXME: Partially
                        pass
                    yield self
                elif order['status'] == ORDER_STATUS_REJECTED:
                    log.error(f'Order {order["orderId"]} is rejected')
                    self.order = order
                    self.state = AddOrder.STATE_ERROR
                    yield self
                elif order['status'] == ORDER_STATUS_EXPIRED:
                    log.warning(f'Order {order["orderId"]} is expired. Retry.')
                    self.state = AddOrder.STATE_ORDER_EXPIRED  # Retry
                    yield self
                    self.state = AddOrder.STATE_ADD_ORDER  # Retry
                elif order['status'] == ORDER_STATUS_NEW:
                    self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
                    yield self
                    # FIXME: si on sait que le stream est done...
                    # await sleep(POOLING_SLEEP)
                else:
                    assert False, f"Unknown status {order['status']}"
            elif self.state == AddOrder.STATE_ORDER_FILLED:
                pass
            elif self.state == AddOrder.STATE_CANCELING:
                try:
                    await client.cancel_order(symbol=self.order['symbol'], orderId=self.order['orderId'])
                except BinanceAPIException as ex:
                    if ex.code == -2011 and ex.message == "Unknown order sent.":
                        pass # Ignore
                    else:
                        raise

                self.state = AddOrder.STATE_CANCELED
                yield self
            elif self.state == AddOrder.STATE_CANCELED:
                return
            else:
                log.error(f'Unknown state \'{self["state"]}\'')
                self.state = AddOrder.STATE_ERROR
                yield self
                return
