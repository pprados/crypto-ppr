# TODO: Conditional to buy after price rises.
# Les conditions c'est si les prix montes.
# J'attend que cela dépasse une resistance, alors je place mon ordre.
import asyncio
import logging
from asyncio import Queue
from pathlib import Path
from typing import Tuple, List

import aiohttp
from aiohttp import ClientConnectorError, ClientError

import global_flags
from TypingClient import TypingClient
from atomic_json import atomic_load_json, atomic_save_json
from binance import AsyncClient
from binance.enums import SIDE_SELL, SIDE_BUY, TIME_IN_FORCE_GTC, ORDER_STATUS_NEW, \
    ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT
from binance.exceptions import BinanceAPIException
from bot_generator import BotGenerator
from conf import EMPTY_PENDING
from shared_time import sleep_speed, get_now
from simulate_client import EndOfDatas, to_str_date
from tools import split_symbol, generate_order_id, \
    update_order, log_wallet, anext, wallet_from_symbol, benefice, alias_symbol, remove_exponent
from .add_order import AddOrder
from .smart_trades_conf import *


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class SmartTrade(BotGenerator):
    # self.last_price c'est le prix de la dernière transaction

    POOLING_SLEEP = 2 * sleep_speed()

    STATE_INIT = "init"
    STATE_TRAILING_BUY = "trailing_buy"
    STATE_COND_MARKET_ORDER = "cond_market_order"
    STATE_COND_LIMIT_ORDER = "cond_limit_order"
    STATE_CREATE_BUY_ORDER = "create_buy_order"
    STATE_WAIT_ADD_ORDER_FILLED = "wait_add_order_filled"
    STATE_BUY_ORDER_FILLED = "buy_order_filled"
    STATE_BUY_ORDER_EXPIRED = "buy_order_expired"

    STATE_TP_ALONE = "tp_alone"
    STATE_WAIT_TP_FILLED = "wait_tp_filled"
    STATE_CHANGE_TP_TO_MARKET = "change_tp_to_market"

    STATE_SL_ALONE = "sl_alone"
    STATE_WAIT_SL_FILLED = "wait_sl_filled"
    STATE_CHANGE_SL_TO_MARKET = "change_sl_to_market"

    STATE_ACTIVATE_TRAILING_SL = "active_sl_condition"
    STATE_SL_TIMEOUT = "active_sl_timeout"
    STATE_SL = "stop_lost"

    STATE_TRAILING = "trailing"
    STATE_ACTIVATE_TAKE_PROFIT = "tp_activate"

    STATE_WAIT_TP_OR_SL_ORDER_FILLED_WITH_WEB_SOCKET = "wait_take_profit_or_stop_loss_web_socket"
    STATE_WAIT_TAKE_PROFIT = "wait_take_profit"

    STATE_FINISH = "finish"
    STATE_CANCELING = "canceling"
    STATE_CANCELED = "canceled"

    # TODO: cancel et autres events users

    def status(self) -> List[Tuple[str, Decimal, str]]:
        # FIXME: ajouter dans les TU
        stat = []
        stat.append(("SL", self.active_stop_loss_condition, ""))
        if self.params.take_profit_trailing:
            stat.append(("TTP", self.active_take_profit_trailing, "Trailing TP"))
            stat.append(("STP", self.active_take_profit_sell, "Trailing Sell TP"))
        else:
            stat.append(("TP", self.active_take_profit_condition, "TP"))

        if self.params.take_profit_minimal and 'activate_min_tp' in self:
            stat.append(("MTP", self.mtp_start_delay, "TP"))

        if self.buy_order:
            stat.append(("Buy", self.buy_order.price, "BUY"))

        pass  # TODO

    async def generator(self,
                        client: AsyncClient,
                        engine: 'Engine',
                        queue: Queue,
                        log: logging,
                        init: Dict[str, str],  # Initial context
                        client_account: Dict[str, Any],
                        generator_name: str,
                        conf: Dict[str, Any],
                        **kwargs) -> None:

        self.old = Decimal("0")  # FIXME: a virer

        # TODO: les modes c'est ici : https://help.3commas.io/en/articles/3109037-what-s-the-best-way-to-follow-a-price
        # basé sur order book
        def check_error(msg: Dict[str, Any]) -> None:
            if 'e' in msg and msg['e'] == "error":
                # Web socket in error
                log.error("Web socket for market in error")
                raise ClientError(msg['m'])

        async def trailing_buy(msg: Dict[str, Any]) -> None:
            if msg["_stream"].endswith("@trade") and \
                    msg['e'] == "trade" and msg['s'] == params.symbol:
                trade_price = Decimal(msg['p'])
                # log.info(f"{trade_price=}")
                if not self.activate_trailing_buy and trade_price <= self.active_buy_condition:
                    self.activate_trailing_buy = True
                if self.activate_trailing_buy:
                    if trade_price > self.buy_condition:
                        log.info(f"Price goes up. Buy at market (~{trade_price} {squote})")
                        self.state = SmartTrade.STATE_CREATE_BUY_ORDER  # TODO: market condition
                    else:
                        new_buy_condition = (trade_price * (1 + self.trailing_buy)).normalize()
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
                            if self.active_stop_loss_timer:
                                if self.active_stop_loss_timer + params.stop_loss_timeout < now:
                                    log.debug(f"Timeout = {int(now - self.active_stop_loss_timer)}")
                                    if trigger_price < self.buy_order.price:  # TODO: add fees
                                        await engine.send_telegram(log,
                                                                   f"****** Activate TSL... (current={trigger_price:+})")
                                    else:
                                        await engine.send_telegram(log,
                                                                   f"****** Activate MTP... (current={trigger_price:+})")
                                    self.stop_loss_trigger_price = trigger_price
                                    self.state = SmartTrade.STATE_SL
                            else:
                                self.active_stop_loss_timer = get_now()
                                log.debug(f"SL start timer... {to_str_date(self.active_stop_loss_timer)}")
                        else:
                            if trigger_price < self.buy_order.price:  # TODO: add fees
                                await engine.send_telegram(log, "****** Activate TSL...")
                            else:
                                await engine.send_telegram(log, "****** Activate Mini-TP...")
                            self.stop_loss_trigger_price = trigger_price
                            self.state = SmartTrade.STATE_SL
                    else:
                        if trigger_price < self.buy_order.price:  # TODO: add fees
                            await engine.send_telegram(log, "****** Activate SL...")
                        else:
                            await engine.send_telegram(log, "****** Activate Mini-TP (without trailing)...")
                        self.stop_loss_trigger_price = trigger_price
                        self.state = SmartTrade.STATE_SL
                elif params.stop_loss_trailing:
                    if self.active_stop_loss_timer:
                        log.debug("SL Deactivate timer")
                        self.active_stop_loss_timer = None
                    if trigger_price > self.high_price:
                        new_stop_loss_condition = (trigger_price * (1 + self.stop_loss_percent))
                        if new_stop_loss_condition > self.active_stop_loss_condition:
                            self.active_stop_loss_condition = new_stop_loss_condition
                            log.debug(
                                f"Update TSL trigger condition to {remove_exponent(self.active_stop_loss_condition):+} "
                                f"(current={remove_exponent(trigger_price):+})")
                            if not self.tsl_sup and self.active_stop_loss_condition > self.price:
                                log.warning(f"TSL > buy price {remove_exponent(self.price):+} {sbase}")
                                self.tsl_sup = True

        async def tp_trailing(msg: Dict[str, Any]) -> None:
            trigger_price = None
            if msg["_stream"].endswith("@bookTicker") and \
                    msg["s"] == params.symbol:
                if params.take_profit_base == 'bid':
                    trigger_price = Decimal(msg['b'])
                elif params.take_profit_base == 'ask':
                    trigger_price = Decimal(msg['a'])
                if trigger_price:
                    if self.old != trigger_price:
                        log.debug(f"@bookTicker {trigger_price=}")
                        self.old = trigger_price
            if msg["_stream"].endswith("@trade") and \
                    msg['e'] == "trade" and msg['s'] == params.symbol and \
                    params.take_profit_base == 'last':
                trigger_price = Decimal(msg['p'])

            if trigger_price:
                if params.take_profit_trailing:
                    if not self.active_take_profit_trailing and trigger_price >= self.active_take_profit_condition:
                        log.info("Activate trailing TP...")
                        self.active_take_profit_trailing = True
                    elif self.active_take_profit_trailing:
                        if trigger_price <= self.active_take_profit_sell:
                            log.info(f"Try to TP with ~{trigger_price}...")
                            self.take_profit_trigger_price = trigger_price
                            self.state = SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
                        else:
                            if params.take_profit_trailing < 0:
                                new_take_profit_sell = (trigger_price * (1 + params.take_profit_trailing)).normalize()
                            else:
                                new_take_profit_sell = (trigger_price * (1 + -params.take_profit_trailing)).normalize()
                            if new_take_profit_sell > self.active_take_profit_sell:
                                self.active_take_profit_sell = new_take_profit_sell
                                log.debug(f"Update TP condition = {self.active_take_profit_sell}"
                                          f"(+{(self.active_take_profit_sell / self.buy_order.price - 1) * 100}%)")
                elif trigger_price >= self.active_take_profit_condition:
                    # TP sans trailing
                    log.info(f"Try to TP with {trigger_price} (without trailing)...")
                    self.take_profit_trigger_price = trigger_price
                    self.state = SmartTrade.STATE_ACTIVATE_TAKE_PROFIT

                # Gestion du minimal TP
                if params.take_profit_minimal:
                    if self.mtp_triggered:
                        log.debug(f"{trigger_price=} {self.mtp_target=}")
                    if not self.mtp_activated and self.mtp_triggered and trigger_price < self.mtp_target:
                        # C'est déclenché, et on passe en dessous
                        await engine.send_telegram(
                            log,
                            f"****** Activate MTP because the price {trigger_price} {squote} "
                            f"< {self.mtp_target} {squote} ...")
                        self.take_profit_trigger_price = trigger_price
                        self.state = SmartTrade.STATE_ACTIVATE_TAKE_PROFIT
                        self.mtp_activated = True
                    if not self.mtp_triggered and trigger_price >= self.mtp_target:
                        # Ce n'est pas déclenché, et on passe au dessus...
                        now = get_now()
                        if self.mtp_start_delay:
                            # le timer est actif
                            if self.mtp_start_delay + params.take_profit_minimal_timeout < now:
                                # et je dépasse le délai, donc j'active
                                self.mtp_triggered = True
                                await engine.send_telegram(
                                    log,
                                    f"MTP activated, greater than {self.mtp_target:+} {squote} "
                                    f"for {params.take_profit_minimal_timeout}s")
                                if params.take_profit_trailing_minimal:
                                    self.mtp_target = (self.mtp_target * (1 + params.take_profit_minimal)).normalize()
                                    self.mtp_triggered = False
                                    self.mtp_start_delay = None
                                    await engine.send_telegram(
                                        log,
                                        f"MTP ajusted to {self.mtp_target:+} {squote} "
                                        f"for {params.take_profit_minimal_timeout}s")

                        else:
                            # On active le timer
                            self.mtp_start_delay = now
                    elif not self.mtp_activated:
                        self.mtp_start_delay = None

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
                    "activate_trailing_minimal_take_profit": False,
                    "tsl_sup": False,
                    "take_profit_minimal": Decimal("0"),
                }
            else:
                pass  # Reprise
                self.tsl_sup = False  # FIXME: pour update à chaud
            self.update(init)
            del init

            # ---- Initialisation du bot
            continue_if_partially = True  # FIXME

            # Récupération des paramètres
            params = parse_conf(conf)
            self.params = params

            if EMPTY_PENDING:
                # Clean all orders for this symbol
                await engine.send_telegram(log, f"Clean all pending orders for {params.symbol}")
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
            sbase = alias_symbol(base)
            squote = alias_symbol(quote)
            self.wallet = wallet_from_symbol(client_account, params.symbol)
            self.initial_wallet = self.wallet.copy()

            # ----------------- Reprise du context des sous-generateor après un crash ou un reboot
            if not self:
                # Premier démarrage
                log.info(f"Started")
            else:
                # Reset tout les generateurs
                if 'buy_order' in self and self.buy_order:
                    self.buy_order:Dict[str,Any] = await AddOrder.create(client,
                                                           engine,
                                                           queue,
                                                           log,
                                                           self.buy_order,
                                                           wallet=self.wallet,
                                                           )

                if 'take_profit_order' in self and self.take_profit_order:
                    self.take_profit_order:Dict[str,Any] = await AddOrder.create(client,
                                                                   engine,
                                                                   queue,
                                                                   log,
                                                                   self.take_profit_order,
                                                                   wallet=self.wallet,
                                                                   )

                if 'stop_loss_order' in self and self.stop_loss_order:
                    self.stop_loss_order:Dict[str,Any] = await AddOrder.create(client,
                                                                 engine,
                                                                 queue,
                                                                 log,
                                                                 self.stop_loss_order,
                                                                 wallet=self.wallet,
                                                                 )

            # Finite state machine
            # C'est ici que le bot travaille sans fin, en sauvant sont état à chaque transition
            yield self
            while True:
                # try:
                #     # Reception d'ordres venant de l'API. Par exemple, ajout de fond, arrêt, etc.
                #     msg = bot_queue.get_nowait()
                #     if msg['e'] == 'kill':
                #         await engine.send_telegram(log,"Receive kill")
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
                            self.active_buy_condition = (base_price * (1 + params.trailing_buy)).normalize()
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
                                self.active_buy_condition = (price * (1 + self.trailing_buy))  # .normalize()

                            self.buy_condition = (price * (1 + self.trailing_buy))  # .normalize()
                            self.activate_trailing_buy = True
                        log.info(f"Activate trailing buy at {self.buy_condition} {squote}")
                        self.state = SmartTrade.STATE_TRAILING_BUY
                    else:
                        if params.mode == COND_MARKET_ORDER:
                            self.state = SmartTrade.STATE_COND_MARKET_ORDER
                        elif params.mode == COND_LIMIT_ORDER:
                            self.state = SmartTrade.STATE_COND_LIMIT_ORDER
                        else:
                            self.state = SmartTrade.STATE_CREATE_BUY_ORDER
                    yield self

                elif self.state in [SmartTrade.STATE_COND_MARKET_ORDER, SmartTrade.STATE_COND_LIMIT_ORDER]:
                    msg = await queue.get()
                    check_error(msg)
                    if msg["_stream"].endswith("@trade") and \
                            msg['e'] == "trade" and msg['s'] == params.symbol:
                        trade_price = Decimal(msg['p'])
                        log.debug(f"{trade_price=}")
                        if trade_price >= params.cond_price:
                            self.state = SmartTrade.STATE_CREATE_BUY_ORDER
                            log.info(f"Price >= {params.cond_price}. Activate buy order")
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
                        if params.mode == "LIMIT":
                            order["quantity"] = (self.wallet[quote] * params.size) / params.price
                        else:
                            order["quoteOrderQty"] = self.wallet[quote] * params.size
                    elif params.total:
                        if params.mode == "LIMIT":
                            order["quantity"] = params.total / params.price
                        else:
                            order["quoteOrderQty"] = params.total

                    if params.trailing_buy or params.mode in [MARKET, COND_MARKET_ORDER]:
                        order["type"] = ORDER_TYPE_MARKET
                    elif params.mode in [LIMIT, COND_LIMIT_ORDER]:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        order["price"] = remove_exponent(params.price)
                    else:
                        raise ValueError("type error")

                    # Ajuste le prix d'un ordre
                    if 'price' in order:  # quote_qty ?
                        order = update_order(symbol_info, current_price, order)
                    # Mémorise l'ordre pour pouvoir le rejouer si nécessaire
                    self.buy_order = await AddOrder.create(
                        client,
                        engine,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=True
                    )
                    self.state = SmartTrade.STATE_WAIT_ADD_ORDER_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_ADD_ORDER_FILLED:
                    await anext(self.buy_order)
                    if self.buy_order.is_error():
                        self._set_state_error()
                    elif self.buy_order.is_filled():
                        self.state = SmartTrade.STATE_BUY_ORDER_FILLED
                    yield self


                # ---------------- Aiguillage suivant les cas
                elif self.state == SmartTrade.STATE_BUY_ORDER_FILLED:
                    if not params.use_take_profit and not params.use_stop_loss:
                        # Rien à faire après l'achat
                        self.state = SmartTrade.STATE_FINISH
                    else:
                        if params.use_stop_loss:
                            self.high_price = self.buy_order.price
                            percent = params.stop_loss_percent
                            if params.stop_loss_limit:
                                if params.stop_loss_limit < 0:  # Indique la perte acceptable en volume
                                    # Calcul du stop price, pour ne perde que stop_loss_limit
                                    stop_loss_limit = (
                                        -(-params.stop_loss_limit / self.buy_order.quantity - self.buy_order.price))
                                    percent = stop_loss_limit / self.buy_order.price - 1
                                else:
                                    percent = params.stop_loss_limit / self.buy_order.price - 1
                            self.stop_loss_percent = percent
                            self.active_stop_loss_condition = (self.buy_order.price * (1 + percent)).normalize()
                            assert self.active_stop_loss_condition < self.buy_order.price
                            self.active_stop_loss_timer = None
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
                                        f"Set TSL condition to {self.active_stop_loss_condition:+} ({percent * 100:+}% / "
                                        f"{remove_exponent(-(self.buy_order.price * self.buy_order.quantity * -percent)):+} "
                                        f"{squote} / {params.stop_loss_base})")
                                else:
                                    log.info(
                                        f"Set SL condition to {self.active_stop_loss_condition:+} "
                                        f"({params.stop_loss_base})")
                                self.state = SmartTrade.STATE_TRAILING

                        if params.use_take_profit:
                            self.high_price = self.buy_order.price
                            if params.take_profit_minimal:
                                self.mtp_target = (self.buy_order.price * (1 + params.take_profit_minimal)).normalize()
                                self.mtp_triggered = False
                                self.mtp_activated = False
                                self.mtp_start_delay = None
                                log.info(
                                    f"Set MTP trigger condition to "
                                    f"{self.mtp_target:+} {squote} ({remove_exponent(params.take_profit_minimal * 100):+}% /"
                                    f" {params.take_profit_base})")
                            if not params.take_profit_trailing \
                                    and not params.take_profit_minimal \
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
                                tp_price = (self.buy_order.price * (1 + percent)).normalize()

                                if params.take_profit_trailing:
                                    if params.take_profit_trailing < 0:
                                        self.active_take_profit_condition = tp_price
                                        self.active_take_profit_sell = self.active_take_profit_condition * (
                                                1 + params.take_profit_trailing)
                                    else:
                                        self.active_take_profit_condition = \
                                            (tp_price * (1 + params.take_profit_trailing)).normalize()
                                        self.active_take_profit_sell = tp_price
                                    tp_sell_condition = (self.active_take_profit_sell / self.buy_order.price - 1) * 100
                                    assert self.active_take_profit_sell < self.active_take_profit_condition
                                    assert tp_sell_condition > 0
                                    log.info(
                                        f"Set TTP trigger condition to "
                                        f"{self.active_take_profit_condition:+} {squote}"
                                        f" ({remove_exponent(((self.active_take_profit_condition / self.buy_order.price) - 1) * 100):+}%)"
                                        f" ({params.take_profit_base})")
                                    log.info(
                                        f"Set TTP sell condition to {self.active_take_profit_sell:+} {squote}"
                                        f" ({remove_exponent(tp_sell_condition):+}%)")
                                else:
                                    self.active_take_profit_condition = tp_price
                                    log.info(
                                        f"Set TP trigger condition to {self.active_take_profit_condition} {squote}"
                                        f" ({remove_exponent((self.active_take_profit_condition / self.buy_order.price - 1) * 100):+}%)")

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
                                price = (self.buy_order.price * (1 + params.take_profit_limit_percent)).normalize()
                            else:
                                price = params.take_profit_limit
                            order["price"] = remove_exponent(price)
                            order["stopPrice"] = remove_exponent(price)
                            order["quantity"] = self.buy_order.quantity
                        else:
                            assert False, "Trouve une alternative"
                    elif params.take_profit_mode == LIMIT:
                        order["type"] = ORDER_TYPE_TAKE_PROFIT_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        if params.take_profit_limit_percent:
                            price = (self.buy_order.price * (1 + params.take_profit_limit_percent)).normalize()
                        else:
                            price = params.take_profit_limit
                        order["price"] = remove_exponent(price)
                        order["stopPrice"] = remove_exponent(params.take_profit_limit)
                        order["quantity"] = self.buy_order.quantity
                    else:
                        assert False, f"Invalid {params.take_profit_limit}"
                    order = update_order(symbol_info, None, order)
                    self.take_profit_order = await AddOrder.create(
                        client,
                        engine,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=True
                    )
                    self.state = SmartTrade.STATE_WAIT_TP_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_TP_FILLED:
                    if params.take_profit_sell_timeout and 'take_profit_order_timeout' not in self:
                        self.take_profit_order_timeout = get_now() + params.take_profit_sell_timeout
                    await anext(self.take_profit_order)
                    if self.take_profit_order.is_error():
                        self._set_state_error()
                    elif self.take_profit_order.is_filled():
                        self.state = SmartTrade.STATE_FINISH
                    elif self.take_profit_order.order['type'] == ORDER_TYPE_LIMIT and \
                            'take_profit_order_timeout' in self:
                        if self.take_profit_order_timeout < get_now():
                            await engine.send_telegram(log, "Timeout for sell with LIMIT")
                            del self.take_profit_order_timeout
                            self.state = SmartTrade.STATE_CHANGE_TP_TO_MARKET
                    yield self

                elif self.state == SmartTrade.STATE_CHANGE_TP_TO_MARKET:
                    # S'assure d'avoir valider l'ordre précédent
                    await self.take_profit_order.cancel()
                    await anext(self.take_profit_order)
                    if self.take_profit_order.is_canceled():
                        # Ce n'est pas trop tard
                        origin_order = self.take_profit_order.order
                        if "status" not in origin_order or origin_order["status"] == ORDER_STATUS_NEW:
                            order = {
                                "newClientOrderId": generate_order_id(generator_name),
                                "symbol": origin_order["symbol"],
                                "side": origin_order["side"],
                                "type": ORDER_TYPE_MARKET,
                                "quantity": origin_order["quantity"],
                            }
                            self.take_profit_order = await AddOrder.create(
                                client,
                                engine,
                                queue,
                                log,
                                order=order,
                                wallet=self.wallet,
                                continue_if_partially=True
                            )
                            await engine.send_telegram(log, "Re push order with MARKET price")
                    self.state = SmartTrade.STATE_WAIT_TP_FILLED
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
                                order["price"] = remove_exponent(
                                    self.active_stop_loss_condition)  # TODO: trouver explication
                                order["stopPrice"] = remove_exponent(self.active_stop_loss_condition)
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
                                stop_price = (self.buy_order.price * (1 + params.stop_loss_percent)).normalize()
                            else:
                                stop_price = params.stop_loss_limit
                            order["price"] = remove_exponent(stop_price)  # TODO: trouver explication
                            order["stopPrice"] = remove_exponent(stop_price)
                            order["quantity"] = self.buy_order.quantity
                        else:
                            # TODO: si params.stop_loss_base != "last", à la bots_engine
                            assert False, "Trouve une alternative"
                    else:
                        assert False, f"Invalid {params.stop_loss_mode}"
                    order = update_order(symbol_info, None, order)
                    self.stop_loss_order = await AddOrder.create(
                        client,
                        engine,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=True
                    )
                    self.state = SmartTrade.STATE_WAIT_SL_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_WAIT_SL_FILLED:
                    if params.stop_loss_sell_timeout and 'stop_loss_order_timeout' not in self:
                        self.stop_loss_order_timeout = get_now() + params.stop_loss_sell_timeout
                    await anext(self.stop_loss_order)
                    if self.stop_loss_order.is_error():
                        self._set_state_error()
                    elif self.stop_loss_order.is_filled():
                        if "origQty" in self.stop_loss_order.order and \
                                Decimal(self.stop_loss_order.order["origQty"]) != Decimal(
                            self.stop_loss_order.quantity):
                            log.info("Partially sell")
                        self.state = SmartTrade.STATE_FINISH  # TODO: autres erreurs
                    elif self.stop_loss_order.order['type'] == ORDER_TYPE_LIMIT and \
                            'stop_loss_order_timeout' in self:
                        if self.stop_loss_order_timeout < get_now():
                            await engine.send_telegram(log, "Timeout for sell with LIMIT")
                            self.state = SmartTrade.STATE_CHANGE_SL_TO_MARKET
                    yield self

                elif self.state == SmartTrade.STATE_CHANGE_SL_TO_MARKET:
                    # S'assure d'avoir valider l'ordre précédent
                    await self.stop_loss_order.cancel()
                    await anext(self.stop_loss_order)
                    if self.stop_loss_order.is_canceled():
                        # Ce n'est pas trop tard, je peux l'annuler
                        origin_order = self.stop_loss_order.order
                        if "status" not in origin_order or origin_order["status"] == ORDER_STATUS_NEW:
                            order = {
                                "newClientOrderId": generate_order_id(generator_name),
                                "symbol": origin_order["symbol"],
                                "side": origin_order["side"],
                                "type": ORDER_TYPE_MARKET,
                                "quantity": origin_order["quantity"],
                            }
                            self.stop_loss_order = await AddOrder.create(
                                client,
                                engine,
                                queue,
                                log,
                                order=order,
                                wallet=self.wallet,
                                continue_if_partially=True
                            )
                            await engine.send_telegram(log, "Re push SL order with MARKET price")
                    self.state = SmartTrade.STATE_WAIT_SL_FILLED
                    yield self


                # ---------------- trailing
                elif self.state == SmartTrade.STATE_TRAILING:

                    msg = await queue.get()
                    check_error(msg)
                    s = self.state
                    if params.use_take_profit:
                        await tp_trailing(msg)  # First, try to TP
                    # In second step, priority of stop loss ?
                    if params.use_stop_loss:
                        await sl_trailing(msg)
                    # In last step, Adjust the high price
                    if msg["_stream"].endswith("@trade") and \
                            msg['e'] == "trade" and msg['s'] == params.symbol:
                        trigger_price = Decimal(msg['p'])
                        if trigger_price > self.high_price:
                            self.high_price = trigger_price

                    yield self
                elif self.state == SmartTrade.STATE_SL:
                    # Add trade to stop loss

                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                        "quantity": self.buy_order.quantity
                    }

                    if params.stop_loss_order_price:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        order["price"] = remove_exponent(params.stop_loss_order_price)
                    else:
                        if params.stop_loss_mode_sell == ORDER_TYPE_LIMIT:
                            order["type"] = ORDER_TYPE_LIMIT
                            order["price"] = \
                                remove_exponent(
                                    (self.stop_loss_trigger_price * (1 + params.stop_loss_mode_sell_percent)))
                            order["timeInForce"] = TIME_IN_FORCE_GTC
                        else:
                            order["type"] = ORDER_TYPE_MARKET
                    order = update_order(symbol_info, None, order)
                    prefix = "SL:"
                    if order["type"] != ORDER_TYPE_MARKET and Decimal(order["price"]) > self.buy_order.price:
                        prefix = "TP:"
                    self.stop_loss_order = await AddOrder.create(
                        client,
                        engine,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=True,
                        prefix=prefix
                    )
                    self.state = SmartTrade.STATE_WAIT_SL_FILLED
                    yield self

                elif self.state == SmartTrade.STATE_ACTIVATE_TAKE_PROFIT:
                    # Take profit apres un trailing
                    if params.take_profit_mode_sell == MARKET:
                        order = {
                            "newClientOrderId": generate_order_id(generator_name),
                            "symbol": params.symbol,
                            "side": SIDE_SELL,
                            "type": ORDER_TYPE_MARKET,
                            "quantity": self.buy_order.quantity
                        }
                    else:
                        order = {
                            "newClientOrderId": generate_order_id(generator_name),
                            "symbol": params.symbol,
                            "side": SIDE_SELL,
                            "type": ORDER_TYPE_LIMIT,
                            "price":
                                remove_exponent((self.take_profit_trigger_price * (
                                        1 + params.take_profit_mode_sell_percent))),
                            "timeInForce": TIME_IN_FORCE_GTC,
                            "quantity": self.buy_order.quantity
                        }

                    order = update_order(symbol_info, None, order)
                    self.take_profit_order = await AddOrder.create(
                        client,
                        engine,
                        queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=True,
                        prefix="TAKE PROFIT:"
                    )
                    self.state = SmartTrade.STATE_WAIT_TP_FILLED
                    yield self

                # ---------------- End
                elif self.state == SmartTrade.STATE_FINISH:
                    log.info("Smart Trade finished")
                    await benefice(engine, log, self.wallet, self.initial_wallet)
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
                    self.running = False
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


        except (ClientError, ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
            # Attention, pas de sauvegarde.
            raise ex


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
    # Puis initialisation du generateur
    bot_generator = await SmartTrade.create(client,
                                            engine,
                                            bot_queue,
                                            log,
                                            state_for_generator,
                                            generator_name=bot_name,
                                            client_account=client_account,
                                            conf=conf,
                                            )
    try:
        while True:
            rc = await anext(bot_generator)
            if not global_flags.simulate:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                atomic_save_json(bot_generator, path)
            if rc == bot_generator.STATE_FINISHED:
                break
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.initial_wallet, prefix="Before:")
        log_wallet(log, bot_generator.wallet)
        raise
