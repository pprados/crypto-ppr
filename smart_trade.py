# TODO: Conditional to buy after price rises.
# Les conditions c'est si les prix montes.
# J'attend que cela dépasse une resistance, alors je place mon ordre.
import asyncio
import logging
from asyncio import Queue, sleep
from pathlib import Path

import aiohttp
from aiohttp import ClientConnectorError
from binance import AsyncClient
from binance.enums import SIDE_SELL, SIDE_BUY, TIME_IN_FORCE_GTC, ORDER_STATUS_NEW, \
    ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT
from binance.exceptions import BinanceAPIException

import global_flags
from TypingClient import TypingClient
from add_order import AddOrder
from bot_generator import BotGenerator
from conf import EMPTY_PENDING
from events_queues import EventQueues
from shared_time import sleep_speed, get_now
from simulate_client import EndOfDatas, to_str_date
from smart_trades_conf import *
from tools import split_symbol, atomic_load_json, generate_order_id, \
    update_order, log_wallet, atomic_save_json, get_order_price


def _benefice(log: logging, wallet: Dict[str, Decimal], initial_wallet: Dict[str, Decimal]) -> None:
    diff = {k: float(v - initial_wallet[k]) for k, v in wallet.items()}
    # log.info(f"###### Result: {wallet[base] - base_solde} {base} / {wallet[quote] - quote_solde} {quote}")
    log.info(f"###### Result: {diff}")


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class SmartTrade(BotGenerator):
    # self.last_price c'est le prix de la dernière transaction

    POOLING_SLEEP = 2 * sleep_speed()

    STATE_INIT = "init"
    STATE_TRAILING_BUY = "trailing_buy"
    STATE_CREATE_BUY_ORDER = "create_buy_order"
    STATE_WAIT_ADD_ORDER_FILLED = "wait_add_order_filled"
    STATE_BUY_ORDER_FILLED = "buy_order_filled"
    STATE_BUY_ORDER_EXPIRED = "buy_order_expired"

    STATE_TP_ALONE = "tp_alone"
    STATE_WAIT_TP_FILLED = "wait_tp_filled"

    STATE_SL_ALONE = "sl_alone"
    STATE_WAIT_SL_FILLED = "wait_sl_filled"

    STATE_ACTIVATE_TRAILING_SL = "active_sl_condition"
    STATE_SL_TIMEOUT = "active_sl_timeout"
    STATE_SL = "stop_lost"

    STATE_TRAILING = "trailing"
    STATE_ACTIVATE_TAKE_PROFIT = "tp_activate"

    STATE_WAIT_TP_OR_SL_ORDER_FILLED_WITH_WEB_SOCKET = "wait_take_profit_or_stop_loss_web_socket"
    STATE_WAIT_TAKE_PROFIT = "wait_take_profit"

    STATE_FINISH = "finish"
    STATE_FINISHED = BotGenerator.FINISHED
    STATE_ERROR = BotGenerator.ERROR
    STATE_CANCELING = "canceling"
    STATE_CANCELED = "canceled"

    def is_error(self):
        return self.state == SmartTrade.STATE_ERROR

    def is_finished(self):
        return self.state == SmartTrade.STATE_FINISHED

    # TODO: cancel

    async def _start(self,
                     client: TypingClient,
                     event_queues: EventQueues,
                     queue: Queue,
                     log: logging,
                     init: Dict[str, str],
                     **kwargs) -> 'WinterSummerBot':
        self._generator = self.generator(client,
                                         event_queues,
                                         queue,
                                         log,
                                         init,
                                         **kwargs)
        await self.next()
        return self

    async def generator(self,
                        client: AsyncClient,
                        event_queues: EventQueues,
                        queue: Queue,
                        log: logging,
                        init: Dict[str, str],  # Initial context
                        client_account: Dict[str, Any],
                        generator_name: str,
                        conf: Dict[str, Any],
                        **kwargs) -> None:

        # TODO: les modes c'est ici : https://help.3commas.io/en/articles/3109037-what-s-the-best-way-to-follow-a-price
        # basé sur order book
        def check_error(msg: Dict[str, Any]) -> None:
            if 'e' in msg and msg['e'] == "error":
                # Web socket in error
                log.error("Web socket for market in error")
                raise ValueError("Msg error")  # TODO

        async def trailing_buy(msg: Dict[str, Any]) -> None:
            if msg["_stream"].endswith("@trade") and \
                    msg['e'] == "trade" and msg['s'] == params.symbol:
                trade_price = Decimal(msg['p'])
                # log.info(f"{trade_price=}")
                if not self.activate_trailing_buy and trade_price <= self.active_buy_condition:
                    self.activate_trailing_buy = True
                if self.activate_trailing_buy:
                    if trade_price > self.buy_condition:
                        log.info(f"Price goes up. Buy at market")
                        self.state = SmartTrade.STATE_CREATE_BUY_ORDER  # TODO: market condition
                    else:
                        new_buy_condition = trade_price * (1 + self.trailing_buy)
                        if new_buy_condition < self.buy_condition:
                            self.buy_condition = new_buy_condition
                            log.debug(f"Update buy condition = {self.buy_condition}")

        async def sl_trailing(msg: Dict[str, Any]) -> None:
            trigger_price = None
            if msg["_stream"].endswith("@bookTicker") and \
                    msg["s"] == params.symbol:
                if params.stop_loss_base == 'bid':
                    trigger_price = Decimal(msg['b'])
                elif params.stop_loss_base == 'ask':
                    trigger_price = Decimal(msg['a'])
            if msg["_stream"].endswith("@trade") and \
                    msg['e'] == "trade" and msg['s'] == params.symbol:
                trigger_price = Decimal(msg['p'])
            if trigger_price:
                # log.info(f"{trigger_price=}")
                if trigger_price <= self.active_stop_loss_condition:
                    if params.stop_loss_trailing:
                        if params.stop_loss_timeout:
                            now = get_now()
                            if self.active_stop_loss_timeout:
                                if self.active_stop_loss_timeout + params.stop_loss_timeout > now:
                                    log.warning("****** Activate STOP-LOSS...")
                                    self.state = SmartTrade.STATE_SL
                            else:
                                self.active_stop_loss_timeout = get_now()
                                log.debug(f"Stop-loss start timer... {to_str_date(self.active_stop_loss_timeout)}")
                        else:
                            log.warning("****** Activate STOP-LOSS...")
                            self.state = SmartTrade.STATE_SL
                    else:
                        log.warning("****** Activate STOP-LOSS (without trailing)...")
                        self.state = SmartTrade.STATE_SL
                elif params.stop_loss_trailing:
                    if self.active_stop_loss_timeout:
                        log.debug("Deactivate stop-loss timeout")
                        self.active_stop_loss_timeout = None
                    new_stop_loss_condition = trigger_price * (1 + self.stop_loss_percent)
                    if new_stop_loss_condition > self.active_stop_loss_condition:
                        self.active_stop_loss_condition = new_stop_loss_condition
                        log.debug(f"Update stop lost condition = {self.active_stop_loss_condition}")

        async def tp_trailing(msg: Dict[str, Any]) -> None:
            trigger_price = None
            if msg["_stream"].endswith("@bookTicker") and \
                    msg["s"] == params.symbol:
                if params.take_profit_base == 'bid':
                    trigger_price = Decimal(msg['b'])
                elif params.take_profit_base == 'ask':
                    trigger_price = Decimal(msg['a'])
            if msg["_stream"].endswith("@trade") and \
                    msg['e'] == "trade" and msg['s'] == params.symbol \
                    and params.take_profit_base == 'last':
                trigger_price = Decimal(msg['p'])
            if trigger_price:
                # log.info(f"{trigger_price=}")
                if params.take_profit_trailing:
                    if not self.active_take_profit_trailing and trigger_price >= self.active_take_profit_condition:
                        log.info("Activate trailing TP...")
                        self.active_take_profit_trailing = True
                    elif self.active_take_profit_trailing:
                        if trigger_price <= self.active_take_profit_sell:
                            log.info(f"Try to take-profit with {trigger_price}...")
                            self.state = SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
                        else:
                            if params.take_profit_trailing < 0:
                                new_take_profit_sell = trigger_price * (1 + params.take_profit_trailing)
                            else:
                                new_take_profit_sell = trigger_price * (1 + -params.take_profit_trailing)
                            if new_take_profit_sell > self.active_take_profit_sell:
                                self.active_take_profit_sell = new_take_profit_sell
                                log.debug(f"Update take-profit condition = {self.active_take_profit_sell}"
                                          f"(+{(self.active_take_profit_sell / self.buy_order.price - 1) * 100}%)")
                elif trigger_price >= self.active_take_profit_condition:
                    # TP sans trailing
                    log.info(f"Try to take-profit with {trigger_price} (without trailing)...")
                    self.state = SmartTrade.STATE_ACTIVATE_TAKE_PROFIT

        try:
            if not init:
                # Premier départ
                now = get_now()
                init = {
                    "bot_start": now,
                    "bot_last_update": now,
                    "bot_stop": None,
                    "running": True,
                    "state": SmartTrade.STATE_INIT,
                    "order_state": None,
                    "wallet": {},
                    "last_price": Decimal(0),
                    "activate_trailing_buy": False,
                    "activate_trailing_take_profit": False,
                    "activate_trailing_stop_loss": False,
                }
            self.update(init)
            del init

            # ---- Initialisation du bot
            continue_if_partially = True  # FIXME

            # Récupération des paramètres
            params = parse_conf(conf)

            if EMPTY_PENDING:
                # Clean all orders for this symbol
                self.log.warning(f"Clean all pending orders for {params.symbol}")
                for order in await client.get_open_orders(symbol=params.symbol):
                    if order["status"] == ORDER_STATUS_NEW:
                        await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

            # ---- Gestion des queues de communications asynchones
            # add_multiplex_socket(params.symbol.lower() + "@trade", put_mixte)
            # add_multiplex_socket(params.symbol.lower() + "@bookTicker", put_mixte)

            # ---- Analyse des paramètres
            # Récupération des contraintes des devices
            symbol_info = await client.get_symbol_info(params.symbol)

            # Récupération des balances du wallet master
            base, quote = split_symbol(params.symbol)
            balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
            balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))
            self.wallet[base] = balance_base["free"]
            self.wallet[quote] = balance_quote["free"]
            self.initial_wallet = self.wallet.copy()

            # ----------------- Reprise du context des sous-generateor après un crash ou un reboot
            if not self:
                # Premier démarrage
                log.info(f"Started")
            else:
                # Reset les sous generateur
                if 'buy_order' in self and self.buy_order:
                    self.buy_order = await AddOrder.reset(client,
                                                          event_queues,
                                                          queue,
                                                          log,
                                                          self.buy_order,
                                                          wallet=self.wallet,
                                                          )

                if 'take_profit_order' in self and self.take_profit_order:
                    self.take_profit_order = await AddOrder.reset(client,
                                                                  event_queues,
                                                                  queue,
                                                                  log,
                                                                  self.take_profit_order,
                                                                  wallet=self.wallet,
                                                                  )

                if 'stop_loss_order' in self and self.stop_loss_order:
                    self.stop_loss_order = await AddOrder.reset(client,
                                                                event_queues,
                                                                queue,
                                                                log,
                                                                self.stop_loss_order,
                                                                wallet=self.wallet,
                                                                )

            # Finite state machine
            # C'est ici que le bot travaille sans fin, en sauvant sont état à chaque transition
            while True:
                # try:
                #     # Reception d'ordres venant de l'API. Par exemple, ajout de fond, arrêt, etc.
                #     msg = bot_queue.get_nowait()
                #     if msg['e'] == 'kill':
                #         log.warning("Receive kill")
                #         return
                # except QueueEmpty:
                #     pass  # Ignore

                if self.state == SmartTrade.STATE_INIT:
                    if params.trailing_buy:
                        if params.trailing_buy < 0:
                            if not params.price:
                                base_price = (await client.get_symbol_ticker(symbol=params.symbol))["price"]
                            else:
                                base_price = params.price
                            self.trailing_buy = -params.trailing_buy
                            self.active_buy_condition = base_price * (1 + params.trailing_buy)
                            self.buy_condition = self.active_buy_condition
                            self.activate_trailing_buy = False
                        else:
                            self.trailing_buy = params.trailing_buy
                            self.active_buy_condition = params.price
                            if params.mode == MARKET:
                                price = (await client.get_symbol_ticker(symbol=params.symbol))["price"]
                                self.active_buy_condition = price
                            else:
                                price = params.price
                                self.active_buy_condition = price * (1 + self.trailing_buy)

                            self.buy_condition = price * (1 + self.trailing_buy)
                            self.activate_trailing_buy = True
                        log.info(f"Activate trailing buy at {self.buy_condition} {quote}")
                        self.state = SmartTrade.STATE_TRAILING_BUY
                    else:
                        self.state = SmartTrade.STATE_CREATE_BUY_ORDER
                    yield self

                elif self.state == SmartTrade.STATE_TRAILING_BUY:
                    msg = await queue.get()
                    check_error(msg)
                    t = self.state
                    await trailing_buy(msg)
                    if t != self.state:
                        yield self

                elif self.state == SmartTrade.STATE_CREATE_BUY_ORDER:
                    # Place un ordre BUY

                    self.activate_trailing_buy = False
                    current_price = (await client.get_symbol_ticker(symbol=params.symbol))["price"]

                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_BUY,
                    }

                    if params.unit:
                        quantity = params.unit  # TODO: unit ou quote ?
                        order["quantity"] = quantity
                    elif params.size:
                        order["quoteOrderQty"] = balance_quote['free'] * params.size
                    elif params.total:
                        order["quoteOrderQty"] = params.total

                    if params.trailing_buy or params.mode == MARKET:
                        order["type"] = ORDER_TYPE_MARKET
                    elif params.mode == LIMIT:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        order["price"] = params.price
                    elif params.mode == COND_LIMIT_ORDER:
                        # TODO Will be placed on the exchange order book when the conditional order triggers
                        pass
                    elif params.mode == COND_MARKET_ORDER:
                        # TODO Will be placed on the exchange order book when the conditional order triggers
                        pass
                    else:
                        raise ValueError("type error")

                    # Ajuste le prix d'un ordre
                    if 'price' in order:  # quote_qty ?
                        order = update_order(symbol_info, current_price, order)
                    # Mémorise l'ordre pour pouvoir le rejouer si nécessaire
                    self.buy_order = await AddOrder.create(
                        client,
                        event_queues,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=False
                    )
                    self.state = SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED:
                    await self.buy_order.next()
                    if self.buy_order.is_error():
                        self._set_state_error()
                        yield self
                    elif self.buy_order.is_filled():
                        self.state = SmartTrade.STATE_BUY_ORDER_FILLED
                        yield self
                    # TODO elif self.buy_order.is_cancelled() etc.


                # ---------------- Aiguillage suivant les cas
                elif self.state == SmartTrade.STATE_BUY_ORDER_FILLED:
                    if not params.use_take_profit and not params.use_stop_loss:
                        # Rien à faire après l'achat
                        self.state = SmartTrade.STATE_FINISH
                    else:
                        if params.use_stop_loss:
                            percent = params.stop_loss_percent
                            if params.stop_loss_limit:
                                percent = params.stop_loss_limit / self.buy_order.price - 1
                            self.stop_loss_percent = percent
                            self.active_stop_loss_condition = self.buy_order.price * (1 + percent)
                            assert self.active_stop_loss_condition < self.buy_order.price
                            self.active_stop_loss_timeout = None
                            if params.use_stop_loss and params.stop_loss_base == "last" \
                                    and not params.use_take_profit \
                                    and not params.stop_loss_trailing and params.use_stop_loss:
                                # SL sans TP ni trailing, sur une base 'last'
                                # Peux utiliser un order TP
                                self.state = SmartTrade.STATE_SL_ALONE
                            else:
                                # SL avec trailing. Ne peux pas utiliser un ordre SL.
                                if params.stop_loss_trailing:
                                    log.info(
                                        f"Set trailing stop-loss condition at {self.active_stop_loss_condition} ({params.stop_loss_base})")
                                else:
                                    log.info(
                                        f"Set stop-loss condition at {self.active_stop_loss_condition} ({params.stop_loss_base})")
                                self.state = SmartTrade.STATE_TRAILING

                        if params.use_take_profit:
                            if not params.take_profit_trailing \
                                    and not params.use_stop_loss \
                                    and params.take_profit_base == "last":  # TODO: si params.take_profit_base != "last", à la bots_engine
                                # TP sans SL ni trailing, sur une base 'last'
                                # Peux utiliser un order TP
                                self.state = SmartTrade.STATE_TP_ALONE
                            else:
                                # TP avec ou sans trailing. Ne peux pas utiliser un ordre TP.
                                percent = params.take_profit_limit_percent
                                if params.take_profit_limit:
                                    percent = (params.take_profit_limit / self.buy_order.price) - 1
                                self.take_profit_percent = percent
                                tp_price = self.buy_order.price * (1 + percent)
                                if params.take_profit_trailing:
                                    if params.take_profit_trailing < 0:
                                        self.active_take_profit_condition = tp_price
                                        self.active_take_profit_sell = self.active_take_profit_condition * (
                                                1 + params.take_profit_trailing)
                                    else:
                                        self.active_take_profit_condition = tp_price * (1 + params.take_profit_trailing)
                                        self.active_take_profit_sell = tp_price
                                    tp_sell_condition=(self.active_take_profit_sell / self.buy_order.price - 1) * 100
                                    assert self.active_take_profit_sell < self.active_take_profit_condition
                                    assert tp_sell_condition > 0
                                    log.info(
                                        f"Set take-profit condition at "
                                        f"{self.active_take_profit_condition} (+{percent * 100}%)"
                                        f" ({params.take_profit_base})")
                                    log.info(
                                        f"Set trailing take profit, sell condition at {self.active_take_profit_sell} ("
                                        f"+{tp_sell_condition}%)")
                                else:
                                    self.active_take_profit_condition = tp_price
                                    log.info(f"Set take profit at {self.active_take_profit_condition} ("
                                             f"+{(self.active_take_profit_condition / self.buy_order.price - 1) * 100}%)")

                                self.active_take_profit_trailing = False
                                self.state = SmartTrade.STATE_TRAILING
                        yield self

                # ---------- Gestion d'un ordre TP seul
                elif self.state == SmartTrade.STATE_TP_ALONE:
                    # Take profit sans stop lost.
                    # Il est possible d'utiliser un ordre pour cela
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                    }
                    # FIXME: base
                    if params.take_profit_mode == MARKET:
                        if ORDER_TYPE_TAKE_PROFIT_LIMIT in symbol_info.orderTypes:
                            order["type"] = ORDER_TYPE_TAKE_PROFIT_LIMIT
                            order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                            if params.take_profit_limit_percent:
                                price = self.buy_order.price * (1 + params.take_profit_limit_percent)
                            else:
                                price = params.take_profit_limit
                            order["price"] = price
                            order["stopPrice"] = price
                            order["quantity"] = self.buy_order.quantity
                        else:
                            assert False, "Trouve une alternative"
                    elif params.take_profit_mode == LIMIT:
                        order["type"] = ORDER_TYPE_TAKE_PROFIT_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        if params.take_profit_limit_percent:
                            price = self.buy_order.price * (1 + params.take_profit_limit_percent)
                        else:
                            price = params.take_profit_limit
                        order["price"] = price
                        order["stopPrice"] = params.take_profit_limit
                        order["quantity"] = self.buy_order.quantity
                    else:
                        assert False, f"Invalid {params.take_profit_limit}"
                    order = update_order(symbol_info, None, order)
                    self.take_profit_order = await AddOrder.create(
                        client,
                        event_queues,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=False
                    )
                    self.state = SmartTrade.STATE_WAIT_TP_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_TP_FILLED:
                    await self.take_profit_order.next()
                    if self.take_profit_order.is_error():
                        self._set_state_error()
                    elif self.take_profit_order.is_filled():
                        self.state = SmartTrade.STATE_FINISH
                    yield self


                # ---------- Gestion d'un ordre SL seul
                elif self.state == SmartTrade.STATE_SL_ALONE:
                    # Stop lost fixe
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                    }
                    if params.stop_loss_mode == MARKET:
                        if params.stop_loss_base == "last":  # TODO: last, ask, bid ?
                            if ORDER_TYPE_STOP_LOSS_LIMIT in symbol_info.orderTypes:
                                order["type"] = ORDER_TYPE_STOP_LOSS_LIMIT
                                order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                                order["price"] = self.active_stop_loss_condition  # TODO: trouver explication
                                order["stopPrice"] = self.active_stop_loss_condition
                                order["quantity"] = self.buy_order.quantity
                            else:
                                assert False, "Trouve une alternative"
                        else:
                            # TODO: si params.stop_loss_base != "last", à la bots_engine
                            assert False, "trouver une alernative"
                    elif params.stop_loss_mode == COND_LIMIT_ORDER:  # FIXME: c'est quoi le "mode" ? C'est le Tracking method selection,
                        if params.stop_loss_base == "last":
                            order["type"] = ORDER_TYPE_STOP_LOSS_LIMIT
                            order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                            if params.stop_loss_percent:
                                stop_price = self.buy_order.price * (1 + params.stop_loss_percent)
                            else:
                                stop_price = params.stop_loss_limit
                            order["price"] = stop_price  # TODO: trouver explication
                            order["stopPrice"] = stop_price
                            order["quantity"] = self.buy_order.quantity
                        else:
                            # TODO: si params.stop_loss_base != "last", à la bots_engine
                            assert False, "Trouve une alternative"
                    else:
                        assert False, f"Invalid {params.stop_loss_mode}"
                    order = update_order(symbol_info, None, order)
                    self.stop_loss_order = await AddOrder.create(
                        client,
                        event_queues,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=False
                    )
                    self.state = SmartTrade.STATE_WAIT_SL_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_SL_FILLED:
                    await self.stop_loss_order.next()
                    if self.stop_loss_order.is_error():
                        self._set_state_error()
                    elif self.stop_loss_order.is_filled():
                        self.state = SmartTrade.STATE_FINISH  # TODO: autres erreurs
                    yield self


                # ---------------- trailing
                elif self.state == SmartTrade.STATE_TRAILING:

                    msg = await queue.get()
                    check_error(msg)
                    s = self.state
                    if params.use_take_profit:
                        await tp_trailing(msg)
                    # In second place, priority of stop loss ?
                    if params.use_stop_loss:
                        await sl_trailing(msg)
                    if s != self.state:
                        yield self
                elif self.state == SmartTrade.STATE_SL:
                    # Add trade to stop loss
                    # TODO: ajouter un order plus tot ?
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "type": ORDER_TYPE_MARKET,
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                        "quantity": self.buy_order.quantity
                    }
                    if params.stop_loss_order_price:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        order["price"] = params.stop_loss_order_price
                    else:
                        order["type"] = ORDER_TYPE_MARKET
                    order = update_order(symbol_info, None, order)
                    self.stop_loss_order = await AddOrder.create(
                        client,
                        event_queues,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=False,
                        prefix="STOP LOSS:"
                    )
                    self.state = SmartTrade.STATE_WAIT_SL_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT:
                    # Take profit apres un trailing
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                        "type": ORDER_TYPE_MARKET,
                        "quantity": self.buy_order.quantity
                    }
                    order = update_order(symbol_info, None, order)
                    self.take_profit_order = await AddOrder.create(
                        client,
                        event_queues,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=False,
                        prefix="TAKE PROFIT:"
                    )
                    self.state = SmartTrade.STATE_WAIT_TP_FILLED
                    yield self

                # ---------------- End
                elif self.state == SmartTrade.STATE_FINISH:
                    log.info("Smart Trade finished")
                    _benefice(log, self.wallet, self.initial_wallet)
                    self._set_terminated()
                    yield self
                elif self.state == SmartTrade.STATE_FINISHED:
                    return
                # ---------------- Cancel and error
                elif self.state == SmartTrade.STATE_CANCELING:
                    try:
                        await client.cancel_order(symbol=self.symbol, orderId=self.buy_order.order['orderId'])
                    except BinanceAPIException as ex:
                        if ex.code == -2011 and ex.message == "Unknown order sent.":
                            pass  # Ignore
                        else:
                            raise
                    self.state = SmartTrade.STATE_CANCELED
                    yield self
                elif self.state == SmartTrade.STATE_CANCELED:
                    return
                elif self.state == SmartTrade.STATE_ERROR:
                    self._set_state_error()
                    return
                else:
                    log.error(f'Unknown state \'{self["state"]}\'')
                    self._set_state_error()
                    yield self
                    return


        except BinanceAPIException as ex:
            if ex.code in (-1013, -2010) and ex.message.startswith("Filter failure:"):
                log.error(ex.message)
                self._set_state_error()
                yield self
            elif ex.code == -2011 and ex.message == "Unknown order sent.":
                log.error(ex.message)
                log.error(self.buy_order.order)
                log.exception(ex)
                self._set_state_error()
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request is outside of the recvWindow.':
                log.error(ex.message)
                self._set_state_error()
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request was 1000ms ahead of the server\'s time.':
                log.error(ex.message)
                self._set_state_error()
                yield self
            else:
                log.exception("Unknown error")
                log.exception(ex)
                self._set_state_error()
                yield self


        except (ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
            self._set_state_error()
            # Attention, pas de sauvegarde.
            raise


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: TypingClient,
              client_account: Dict[str, Any],
              bot_name: str,
              event_queues: EventQueues,
              conf: Dict[str, str]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = event_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    state_for_generator = {}
    if not global_flags.simulate and path.exists():
        state_for_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={state_for_generator['state']}")
    # Puis initialisation du generateur
    bot_generator = await SmartTrade.create(client,
                                            event_queues,
                                            bot_queue,
                                            log,
                                            state_for_generator,
                                            generator_name=bot_name,
                                            client_account=client_account,
                                            conf=conf,
                                            )
    try:
        previous = bot_generator.copy()
        while True:
            rc = await bot_generator.next()
            if not global_flags.simulate:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                if previous != bot_generator:
                    atomic_save_json(bot_generator, path)
                    previous = bot_generator.copy()
            if rc == bot_generator.FINISHED:
                break
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.wallet)
        raise
