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
from tools import log_order, update_wallet, get_order_price, log_add_order
from stream_user import add_user_socket


class AddOrder(BotGenerator):
    POOLING_SLEEP = 2

    STATE_INIT = "init"
    STATE_ADD_ORDER = "add_order"
    STATE_WAIT_ORDER = "wait_order"
    STATE_ORDER_CONFIRMED = "order_confirmed"
    STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET = "wait_order_filled_with_websocket"
    STATE_WAIT_ORDER_FILLED_WITH_POLLING = "wait_order_filled_with_polling"
    STATE_ORDER_FILLED = "order_filled"
    STATE_ORDER_PARTIALLY_FILLED = "order_partially_filled"  # TODO
    STATE_ORDER_EXPIRED = "order_expired"
    STATE_CANCELING = "order_canceling"
    STATE_CANCELED = "order_canceled"
    STATE_ERROR = "order_error"

    @property
    def price(self) -> Decimal:
        return get_order_price(self.order)

    def __repr__(self):
        return self.state+"->"+self.order

    @property
    def quantity(self) -> Decimal:
        if 'executedQty' in self.order:
            return Decimal(self.order['executedQty'])
        else:
            return Decimal(self.order['quantity'])

    async def _start(self,
                     client: AsyncClient,
                     user_queue: Queue,
                     log: logging,
                     init: Dict[str, str],
                     **kwargs) -> 'AddOrder':
        self._generator = self.generator(client,
                                         user_queue,
                                         log,
                                         init,
                                         **kwargs)
        await self.next()
        return self

    def is_filled(self,accept_partial=False):
        return self.state == AddOrder.STATE_ORDER_FILLED or accept_partial \
               and self.state == AddOrder.STATE_ORDER_PARTIALLY_FILLED

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
                        mixte_queue: Queue,
                        log: logging,
                        init: Dict[str, str],
                        **kwargs):
        if not init:
            init = {
                "state": AddOrder.STATE_INIT,
                "order": kwargs['order'],
                "newClientOrderId": kwargs['order']["newClientOrderId"]
            }
        self.update(init)


        def _get_msg():
            return mixte_queue.get()

        cb = kwargs.get('cb')  # Call back pour gérer tous les msgs
        prefix = kwargs.get('prefix','')

        wallet:Dict[str,Decimal] = kwargs['wallet']
        self.continue_if_partially:bool = kwargs.get("continue_if_partially")  # FIXME
        # Resynchronise l'état sauvegardé
        if self.state in (AddOrder.STATE_ORDER_CONFIRMED, AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
        symbol = self.order['symbol']

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
                    new_order = await client.create_order(**self.order)
                except BinanceAPIException as ex:
                    if ex.code == -2010:  # Duplicate order sent ?, ignore
                        if ex.message == "Duplicate order sent.":
                            # Retrouve l'ordre dupliqué.
                            orders = await client.get_all_orders(symbol=symbol)
                            new_order = next(
                                filter(lambda x: x.get("clientOrderId", "") == self.order["newClientOrderId"], orders))
                            # et continue

                        elif ex.message == "Account has insufficient balance for requested action.":
                            self.state = AddOrder.STATE_ERROR
                            yield self
                            continue
                        elif ex.message.startswith("Filter failure:"):
                            raise
                        elif ex.message == "Stop price would trigger immediately.":
                            # FIXME: rejouer ou adapter en market ?
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            log.error(f"{ex.message}")
                            log_order(order)
                            self.state = AddOrder.STATE_ERROR
                            yield self
                            continue
                        elif ex.message == "Stop price would trigger immediately.":
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            raise
                    else:
                        raise

                # C'est bon, il est passé
                new_order["side"] = self.order["side"]
                new_order["type"] = self.order["type"]
                if 'price' in self.order:
                    new_order["price"] = self.order["price"]
                if 'limit' in self.order:
                    new_order["limit"] = self.order["limit"]
                if 'quantity' in self.order:
                    new_order["quantity"] = self.order["quantity"]
                self.order = new_order
                log_add_order(log, new_order, prefix+" Try to ")
                self.state = AddOrder.STATE_ORDER_CONFIRMED
                yield self

            elif self.state == AddOrder.STATE_ORDER_CONFIRMED:
                # Maintenant, il faut attendre son execution effective
                self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET

            elif self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET:
                # Attend en websocket
                try:

                    log.debug("Wait events...")
                    # FIXME: incompatible avec plusieurs ordres en meme temps
                    msg = await wait_for(_get_msg(), timeout=STREAM_MSG_TIMEOUT)
                    # See https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md
                    if msg.get('e') == "error":
                        # Web socket in error
                        self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                        yield self
                    elif msg["_stream"].endswith("@user") and msg['e'] == "executionReport" and \
                            msg['s'] == symbol and \
                            msg['i'] == self['order']['orderId'] and \
                            msg['X'] != ORDER_STATUS_NEW:
                        self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                        yield self
                    # elif msg["_stream"].endswith("@trade") and \
                    #         msg['e'] == "trade" and msg['s'] == self.order['symbol']:
                    #     trade_price = Decimal(msg['p'])
                    #     log.info(f"{trade_price=}")
                    if cb:
                        await cb(msg)
                    mixte_queue.task_done()
                except (asyncio.TimeoutError, Empty) as ex:
                    # Periodiquement, essaye en polling
                    self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
                    yield self
            elif self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING:
                # log.info("Polling get order status...")
                order = await client.get_order(symbol=self.order['symbol'],
                                               orderId=self.order['orderId'])
                # See https://binance-docs.github.io/apidocs/spot/en/#public-api-definitions
                if order['status'] == ORDER_STATUS_FILLED:  # or ORDER_STATUS_IOY_FILLED
                    update_wallet(wallet, order)
                    log_order(log, order, prefix+" ****** ")
                    self.order = order
                    self.state = AddOrder.STATE_ORDER_FILLED
                    yield self
                if order['status'] == ORDER_STATUS_PARTIALLY_FILLED:  # or ORDER_STATUS_PARTIALLY_FILLED
                    # FIXME: Partially a traiter
                    log_order(log,order)
                    update_wallet(wallet, order)
                    log_order(log, order, prefix)
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
                else:
                    assert False, f"Unknown status {order['status']}"
            elif self.state == AddOrder.STATE_ORDER_FILLED:
                return
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
