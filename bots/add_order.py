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
from random import randint
from typing import Dict, Any

from binance.enums import ORDER_STATUS_FILLED, ORDER_STATUS_NEW, ORDER_STATUS_REJECTED, ORDER_STATUS_EXPIRED, \
    ORDER_STATUS_PARTIALLY_FILLED, ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER, ORDER_TYPE_STOP_LOSS, ORDER_TYPE_LIMIT, \
    ORDER_TYPE_STOP_LOSS_LIMIT, ORDER_TYPE_TAKE_PROFIT, ORDER_TYPE_TAKE_PROFIT_LIMIT
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
from shared_time import get_now
from simulate_client import EndOfDatas
from tools import update_wallet, get_order_price, log_add_order, anext, log_wallet, generate_order_id, \
    wallet_from_symbol, remove_exponent


class AddOrder(BotGenerator):
    POOLING_SLEEP = 2

    STATE_INIT = "init"
    STATE_ADD_ORDER = "add_order"
    STATE_ADD_PARTIALLY_ORDER = "add_partially_order"
    STATE_ADD_ORDER_ACCEPTED = "add_order_accepted"
    STATE_RETRY_ADD_ORDER = "retry_add_order"
    STATE_WAIT_ORDER = "wait_order"
    STATE_ORDER_CONFIRMED = "order_confirmed"
    STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET = "wait_order_filled_with_websocket"
    STATE_WAIT_ORDER_FILLED_WITH_POLLING = "wait_order_filled_with_polling"
    STATE_ORDER_FILLED = "order_filled"
    STATE_ORDER_PARTIALLY_FILLED = "order_partially_filled"  # TODO
    STATE_ORDER_EXPIRED = "order_expired"
    STATE_CANCELING = "order_canceling"
    STATE_CANCELED = "order_canceled"

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
        return self.state in (AddOrder.STATE_ORDER_FILLED, AddOrder.STATE_FINISHED) or accept_partial \
               and self.state == AddOrder.STATE_ORDER_PARTIALLY_FILLED

    def is_partially_filled(self):
        return self.state == AddOrder.STATE_ORDER_FILLED and self.order["state"] == ORDER_STATUS_PARTIALLY_FILLED

    def is_waiting(self):
        return self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING

    async def cancel(self):
        while self.state == AddOrder.STATE_ADD_ORDER_ACCEPTED:
            await anext(self)
        if self.state == AddOrder.STATE_ADD_ORDER:
            self.previous_state = self.state
            self.state = AddOrder.STATE_CANCELED
        elif self.state not in (AddOrder.STATE_ORDER_FILLED, AddOrder.STATE_FINISHED, BotGenerator.STATE_FINISHED):
            self.previous_state = self.state
            self.state = AddOrder.STATE_CANCELING

    def is_canceled(self):
        return self.state == AddOrder.STATE_CANCELED

    async def generator(self,
                        client,
                        engine: 'Engine',
                        mixte_queue: Queue,
                        log: logging,
                        init: Dict[str, str],
                        **kwargs):

        if not init:
            now = get_now()
            init = {
                "bot_start": now,
                "bot_last_update": now,
                "bot_stop": None,
                "running": True,
                "state": AddOrder.STATE_INIT,
                "order": kwargs['order'],
                "partially_order": None,
                "partially_qty": Decimal("0"),
                "partially_tx": Decimal("0"),
                "newClientOrderId": kwargs['order'].get("newClientOrderId", generate_order_id("AddOrder"))
            }
        else:
            log.info(f"Restart with state={init['state']}")
        self.update(init)
        del init

        def _get_msg():
            return mixte_queue.get()

        cb = kwargs.get('cb')  # Call back pour gérer tous les msgs
        prefix = kwargs.get('prefix', '')  # Prefix pour les logs

        wallet: Dict[str, Decimal] = kwargs['wallet']
        self.continue_if_partially: bool = kwargs.get("continue_if_partially")
        # Resynchronise l'état sauvegardé
        if self.state in (AddOrder.STATE_ORDER_CONFIRMED, AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET):
            # Si on démarre, il y a le risque d'avoir perdu le message de validation du trade en cours
            # Donc, la première fois, on doit utiliser le pooling
            self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_POLLING
        if self.state == AddOrder.STATE_ADD_ORDER:
            self.state = AddOrder.STATE_RETRY_ADD_ORDER
        symbol = self.order['symbol']

        # Finite state
        yield self  # Une sauvegarde avant de démarrer
        while True:

            if self.state == AddOrder.STATE_INIT:
                self.state = AddOrder.STATE_ADD_ORDER
                yield self
            elif self.state == AddOrder.STATE_RETRY_ADD_ORDER:
                # Detection de doublon, via newClientOrderId
                if "newClientOrderId" in self.order:
                    orders = await client.get_all_orders(symbol=self.order['symbol'], limit=100)
                    order_id = self.order["newClientOrderId"]
                    orders = list(filter(lambda x: x['clientOrderId'] == order_id, orders))
                    if orders:
                        self.new_order = orders[0]
                        self.state = AddOrder.STATE_ADD_ORDER_ACCEPTED
                        yield self
                        continue
                else:
                    await engine.send_telegram(log, "Impossible to detect a duplicate order. May be duplicate.")
                # Pas de doublon, ou détection impossible
                self.state = AddOrder.STATE_ADD_ORDER
            elif self.state in (AddOrder.STATE_ADD_ORDER, AddOrder.STATE_ADD_PARTIALLY_ORDER):
                try:
                    # Prépare la création d'un ordre
                    order = self.partially_order if self.partially_order else self.order
                    await client.create_test_order(**order)
                    # Puis essaye de l'executer
                    log.debug(f"Push   {order}")
                    self.new_order = await client.create_order(**order)
                    log.debug(f"Pushed {self.new_order}")
                    if self.new_order["status"] == ORDER_STATUS_FILLED:
                        update_wallet(wallet, self.new_order)
                        await engine.log_order(log, self.new_order, prefix + " ****** ")
                        self.order = self.new_order
                        self.state = AddOrder.STATE_ORDER_FILLED
                except BinanceAPIException as ex:
                    if ex.code == -2010:  # Duplicate order sent ?, ignore
                        if ex.message == "Duplicate order sent.":
                            # Retrouve l'ordre dupliqué.
                            orders = await client.get_all_orders(symbol=symbol)
                            self.new_order = next(
                                filter(lambda x: x.get("clientOrderId", "") == self.order["newClientOrderId"], orders))
                            # et continue

                        elif ex.message == "Account has insufficient balance for requested action.":
                            self._set_state_error()
                            yield self
                            continue
                        elif ex.message.startswith("Filter failure:"):
                            self._set_state_error()
                            yield self
                            raise
                        elif ex.message == "Stop price would trigger immediately.":
                            # FIXME: rejouer ou adapter en market ?
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            log.error(f"{ex.message}")
                            await engine.log_order(log, new_order)
                            self._set_state_error()
                            yield self
                            continue
                        elif ex.message == "Stop price would trigger immediately.":
                            # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                            raise
                    else:
                        raise
                if self.new_order["status"] != ORDER_STATUS_FILLED:
                    self.state = AddOrder.STATE_ADD_ORDER_ACCEPTED
                yield self

            elif self.state == AddOrder.STATE_ADD_ORDER_ACCEPTED:
                new_order = self.new_order
                del self.new_order
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
                self.original_order = self.order
                self.order = new_order
                self.state = AddOrder.STATE_ORDER_CONFIRMED
                yield self

            elif self.state == AddOrder.STATE_ORDER_CONFIRMED:
                # Maintenant, il faut attendre son execution effective
                self.state = AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET

            elif self.state == AddOrder.STATE_WAIT_ORDER_FILLED_WITH_WEB_SOCKET:
                # Attend en websocket
                try:

                    # log.debug("Wait events...")
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
                        await engine.log_order(log, new_order, prefix + " ****** ")
                        del self._order
                        if self.partially_order:
                            # Création d'un resultat simulé, basé sur l'accumulation des partially_filled
                            self.partially_qty += Decimal(new_order["executedQty"])
                            self.partially_tx += get_order_price(new_order) * Decimal(new_order["executedQty"])
                            new_order["executedQty"] = remove_exponent(self.partially_qty)
                            new_order["price"] = remove_exponent(self.partially_tx / self.partially_qty)
                            new_order["state"] = ORDER_STATUS_FILLED
                        self.order = new_order
                        self.state = AddOrder.STATE_ORDER_FILLED
                        yield self
                    elif new_order['status'] == ORDER_STATUS_PARTIALLY_FILLED:
                        await engine.log_order(log, new_order)
                        update_wallet(wallet, new_order)
                        await engine.log_order(log, new_order, prefix)
                        if not self.continue_if_partially:
                            await engine.send_telegram(
                                log,
                                f"Order {self.order['clientOrderId']} partially executed")
                            del self._order
                            self.order = new_order
                            self.state = AddOrder.STATE_ORDER_FILLED
                        else:
                            self.partially_qty += Decimal(new_order["executedQty"])
                            self.partially_tx += get_order_price(new_order) * Decimal(new_order["executedQty"])
                            await engine.send_telegram(
                                log,
                                f"Order {self.order['clientOrderId']} partially executed, but continue")
                            diff = Decimal(new_order['origQty']) - Decimal(new_order['executedQty'])
                            partially_order = \
                                {k: self.order[k] for k in self.order.keys() & {'symbol', 'side', 'type'}}
                            if partially_order["type"] == ORDER_TYPE_LIMIT:
                                partially_order = \
                                    {**partially_order,
                                     **{k: self.order[k] for k in self.order.keys() & {'timeInForce', 'price'}}}
                            elif partially_order["type"] == ORDER_TYPE_MARKET:
                                partially_order = \
                                    {**partially_order,
                                     **{k: self.order[k] for k in
                                        self.order.keys() & {'quoteOrderQty'}}}
                            elif partially_order["type"] == ORDER_TYPE_STOP_LOSS:
                                partially_order = \
                                    {**partially_order, **{k: self.order[k] for k in self.order.keys() & {'stopPrice'}}}
                            elif partially_order["type"] == ORDER_TYPE_STOP_LOSS_LIMIT:
                                partially_order = \
                                    {**partially_order, **{k: self.order[k] for k in
                                                           self.order.keys() & {'timeInForce', 'price', 'stopPrice'}}}
                            elif partially_order["type"] == ORDER_TYPE_TAKE_PROFIT:
                                partially_order = \
                                    {**partially_order, **{k: self.order[k] for k in self.order.keys() & {'stopPrice'}}}
                            elif partially_order["type"] == ORDER_TYPE_TAKE_PROFIT_LIMIT:
                                partially_order = \
                                    {**partially_order, **{k: self.order[k] for k in
                                                           self.order.keys() & {'timeInForce', 'price', 'stopPrice'}}}
                            elif partially_order["type"] == ORDER_TYPE_LIMIT_MAKER:
                                partially_order = \
                                    {**partially_order,
                                     **{k: self.order[k] for k in self.order.keys() & {'quantity', 'price'}}}

                            partially_order["newClientOrderId"] = self.newClientOrderId + "-" + str(randint(0, 100))
                            if "quoteOrderQty" in self._order:
                                # Ajuste la quantity de quote
                                partially_order["quoteOrderQty"] = remove_exponent(
                                    Decimal(self._order["quoteOrderQty"]) - self.partially_tx)
                            else:
                                partially_order["quantity"] = remove_exponent(diff)
                            try:
                                await client.create_test_order(**partially_order)
                                self.partially_order = partially_order
                                self._order = partially_order  # Pour le retry
                                self.state = AddOrder.STATE_ADD_PARTIALLY_ORDER
                            except BinanceAPIException as ex:
                                if ex.message.startswith("Filter failure:"):
                                    # Can not continue, it's finish
                                    del self._order
                                    self.order = new_order
                                    self.state = AddOrder.STATE_ORDER_FILLED
                                else:
                                    raise

                        yield self
                    elif new_order['status'] == ORDER_STATUS_REJECTED:
                        log.error(f'Order {new_order["orderId"]} is rejected')
                        del self._order
                        self.order = new_order
                        self.state = AddOrder.STATE_ERROR
                        yield self
                    elif new_order['status'] == ORDER_STATUS_EXPIRED:
                        await engine.send_telegram(
                            log,
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
                        await engine.send_telegram(log, "Order does not exist. Retry.")
                        self.state = AddOrder.STATE_ADD_ORDER
                        yield self
                    else:
                        raise

            elif self.state == AddOrder.STATE_ORDER_FILLED:
                self.state = AddOrder.STATE_FINISHED
                yield self
            elif self.state == AddOrder.STATE_FINISHED:
                self.running = False
                return
            elif self.state == AddOrder.STATE_CANCELING:
                try:
                    assert self.order['status']
                    if self.order["status"] == ORDER_STATUS_NEW:
                        assert self.order['orderId']
                        await engine.send_telegram(log, f"Cancel order {self.order['clientOrderId']}")
                        await client.cancel_order(symbol=self.order['symbol'], orderId=self.order['orderId'])
                        self.state = AddOrder.STATE_CANCELED
                    else:
                        log.debug("Trop tard")
                        self.state = AddOrder.STATE_ORDER_FILLED
                except BinanceAPIException as ex:
                    if ex.code == -2011 and ex.message == "Unknown order sent.":
                        pass  # Ignore
                        self.state = AddOrder.STATE_CANCELED
                    else:
                        raise

                yield self
            elif self.state == AddOrder.STATE_CANCELED:
                self.running = False
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
              engine: 'Engine',
              conf: Dict[str, Any]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = engine.event_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    state_for_generator = {}
    if not global_flags.simulate and path.exists():
        state_for_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={state_for_generator['state']}")

    wallet = wallet_from_symbol(client_account, conf['order']['symbol'])
    # Puis initialisation du generateur
    bot_generator = await AddOrder.create(client,
                                          engine,
                                          bot_queue,
                                          log,
                                          state_for_generator,
                                          order=conf['order'],
                                          wallet=wallet,
                                          )
    try:
        while True:
            rc = await anext(bot_generator)
            if not global_flags.simulate:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                atomic_save_json(bot_generator, path)
            if rc == AddOrder.STATE_FINISHED:
                break
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.wallet)
        raise
