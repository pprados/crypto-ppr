"""
Génerateur en charge de la création d'un ordre et de son application.
Le générateur retour le context qu'il faut sauver pour lui dans l'agent.
L'état order_ctx.state fini par etre STATE_ERROR ou STATE_ORDER_FILLED.
"""
import asyncio
import logging
from asyncio import Queue, wait_for
from decimal import Decimal
from pathlib import Path
from queue import Empty
from typing import Dict, Any

from binance.enums import ORDER_STATUS_FILLED, ORDER_STATUS_NEW, ORDER_STATUS_REJECTED, ORDER_STATUS_EXPIRED, \
    ORDER_STATUS_PARTIALLY_FILLED
# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60
from binance.exceptions import BinanceAPIException

import global_flags
from TypingClient import TypingClient
from atomic_json import atomic_load_json, atomic_save_json
from bot_generator import BotGenerator
from conf import STREAM_MSG_TIMEOUT
from events_queues import EventQueues
from simulate_client import EndOfDatas
from tools import log_order, update_wallet, get_order_price, log_add_order, anext, log_wallet, generate_order_id, \
    wallet_from_symbol


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
        return self.state + "->" + self.order

    @property
    def quantity(self) -> Decimal:
        if 'executedQty' in self.order:
            return Decimal(self.order['executedQty'])
        else:
            return Decimal(self.order['quantity'])

    def is_filled(self, accept_partial=False):
        return self.state == AddOrder.STATE_ORDER_FILLED or accept_partial \
               and self.state == AddOrder.STATE_ORDER_PARTIALLY_FILLED

    # FIXME
    # def is_partially_filled(self):
    #     return self.state == AddOrder.STATE_ORDER_FILLED

    def is_waiting(self):
        return self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING

    def cancel(self):
        self.previous_state = self.state
        self.state = AddOrder.STATE_CANCELING

    def is_canceled(self):
        return self.state == AddOrder.STATE_CANCELED

    async def generator(self,
                        client,
                        event_queues: EventQueues,
                        mixte_queue: Queue,
                        log: logging,
                        init: Dict[str, str],
                        **kwargs):
        if not init:
            init = {
                "state": AddOrder.STATE_INIT,
                "order": kwargs['order'],
                "newClientOrderId": kwargs['order'].get("newClientOrderId",generate_order_id("AddOrder"))
            }
        self.update(init)
        del init

        def _get_msg():
            return mixte_queue.get()

        cb = kwargs.get('cb')  # Call back pour gérer tous les msgs
        prefix = kwargs.get('prefix', '')  # Prefix pour les logs

        wallet: Dict[str, Decimal] = kwargs['wallet']
        self.continue_if_partially: bool = kwargs.get("continue_if_partially")  # FIXME
        # Resynchronise l'état sauvegardé
        if self.state in (AddOrder.STATE_ORDER_CONFIRMED, AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
        symbol = self.order['symbol']

        # Finite state
        yield self  # Une sauvegarde avant de démarrer
        while True:

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
                            self.state = AddOrder.STATE_ERROR
                            yield self
                            raise
                        elif ex.message == "Stop price would trigger immediately.":
                            # FIXME: rejouer ou adapter en market ?
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            log.error(f"{ex.message}")
                            log_order(new_order)
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
                log_add_order(log, self.order, prefix + " Try to ")
                self._order = self.order  # Pour le retry
                self.order = new_order
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
                try:
                    new_order = await client.get_order(symbol=self.order['symbol'],
                                                       orderId=self.order['orderId'])
                    # See https://binance-docs.github.io/apidocs/spot/en/#public-api-definitions
                    if new_order['status'] == ORDER_STATUS_FILLED:  # or ORDER_STATUS_IOY_FILLED
                        update_wallet(wallet, new_order)
                        log_order(log, new_order, prefix + " ****** ")
                        del self._order
                        self.order = new_order
                        self.state = AddOrder.STATE_ORDER_FILLED
                        yield self
                    elif new_order['status'] == ORDER_STATUS_PARTIALLY_FILLED:  # or ORDER_STATUS_PARTIALLY_FILLED
                        # FIXME: Partially a traiter
                        log_order(log, new_order)
                        update_wallet(wallet, new_order)
                        log_order(log, new_order, prefix)
                        if not self.continue_if_partially:
                            log.warning("PARTIALLY")
                            del self._order
                            self.order = new_order
                            self.state = AddOrder.STATE_ORDER_FILLED
                        else:
                            # FIXME: Partially
                            pass
                        yield self
                    elif new_order['status'] == ORDER_STATUS_REJECTED:
                        log.error(f'Order {new_order["orderId"]} is rejected')
                        del self._order
                        self.order = new_order
                        self.state = AddOrder.STATE_ERROR
                        yield self
                    elif new_order['status'] == ORDER_STATUS_EXPIRED:
                        log.warning(
                            f'Order {new_order["orderId"]} is expired ({new_order["clientOrderId"]}. Retry')  # FIXME
                        self.order = self._order
                        del self._order
                        self.state = AddOrder.STATE_ORDER_EXPIRED  # Signal the state
                        yield self
                        self.state = AddOrder.STATE_ADD_ORDER  # And retry
                    elif new_order['status'] == ORDER_STATUS_NEW:
                        self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET
                        yield self
                    else:
                        assert False, f"Unknown status {new_order['status']}"
                except BinanceAPIException as ex:
                    if ex.code == -2013 and ex.message.startswith("Order does not exist."):
                        log.warning("Order does not exist. Retry.")
                        self.state = AddOrder.STATE_ADD_ORDER
                        yield self
                    else:
                        raise

            elif self.state == AddOrder.STATE_ORDER_FILLED:
                self.state = AddOrder.STATE_FINISHED
                yield self
                return
            elif self.state == AddOrder.STATE_CANCELING:
                try:
                    await client.cancel_order(symbol=self.order['symbol'], orderId=self.order['orderId'])
                except BinanceAPIException as ex:
                    if ex.code == -2011 and ex.message == "Unknown order sent.":
                        pass  # Ignore
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


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: TypingClient,
              client_account: Dict[str, Any],
              bot_name: str,
              event_queues: EventQueues,
              conf: Dict[str, Any]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = event_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    state_for_generator = {}
    if not global_flags.simulate and path.exists():
        state_for_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={state_for_generator['state']}")

    wallet = wallet_from_symbol(client_account, conf['order']['symbol'])
    # Puis initialisation du generateur
    bot_generator = await AddOrder.create(client,
                                          event_queues,
                                          bot_queue,
                                          log,
                                          state_for_generator,
                                          order=conf['order'],
                                          wallet=wallet,
                                          )
    try:
        previous = None
        while True:
            rc = await anext(bot_generator)
            if not global_flags.simulate:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                if previous != bot_generator:
                    atomic_save_json(bot_generator, path)
                    previous = bot_generator.copy()
            if rc == bot_generator.STATE_FINISHED:
                break
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.wallet)
        raise
