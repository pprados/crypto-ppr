# Si il n'y a pas de TP ou SL, stop après la validion de l'ordre d'achat
# Cas d'un take_profit au market, sans stop_loss, porté par un ordre Binance
# - creation d'un ordre TAKE_PROFIT / MARKET Résilient
#     "smart_trade":  {
#       "symbol": "BTCUSDT",
#       "total": 10,
#       "price": 34100,
#       "mode": "MARKET",
#       "take_profit": {
#         "base": "last", // si market: bid, ask, last
#         "mode": "MARKET",
#         "price": 34100.0, // or "3%"
#       },

# Cas d'un take_profit limit, sans stop_loss, porté par un ordre Binance
# - creation d'un ordre TAKE_PROFIT / LIMIT Résilient
#     "smart_trade":  {
#       "symbol": "BTCUSDT",
#       "total": 10,
#       "price": "1%",
#       "mode": "MARKET",
#       "take_profit": {
#         "base": "last",
#         "mode": "LIMIT", // TODO: Limit a revoir ? ne fonctionne pas
#         "price": 34100.0, // or "3%"
#       },

# Si il y n'y a qu'un stop loss, alors le SmartTrade reste vivant tant que le SL n'est pas activé
# Cas d'un stop_loss seul, porté par un ordre binance
# - creation d'un ordre STOP_LOSS / MARKET Résilient
#     "smart_trade":  {
#       "symbol": "BTCUSDT",
#       "total": 10,
#       "price": 34100,
#       "mode": "MARKET",
#       "stop_loss": {
#         "base": "last",
#         "mode": "MARKET", // "market", "cond_limit"
#         "price": "-1%",
#       }

# Cas d'un trailing stop loss  avec cond_limit (Non résilient)
#     "smart_trade":  {
#       "symbol": "BTCUSDT",
#       "total": 10,
#       "price": 34100,
#       "mode": "MARKET",
#       "stop_loss": {
#         "base": "last", // Non résilient
#         "mode": "COND_LIMIT_ORDER",
#         "price": "-0.01%",
#         "order_price": 35000.0,
#         "timeout": 5,
#         "trailing": true  // Non résilient
#       }

# Cas d'un trailing stop loss  sans cond_limit (Non résilient)
#     "smart_trade":  {
#       "symbol": "BTCUSDT",
#       "total": 10,
#       "price": 34100,
#       "mode": "MARKET",
#       "stop_loss": {
#         "base": "last", // Non résilient
#         "mode": "MARKET",
#         "price": "-0.01%",
#         "timeout": 5,
#         "trailing": true  // Non résilient
#       }

import asyncio
import logging
from asyncio import Queue, QueueEmpty, wait_for, sleep
from pathlib import Path
from queue import Empty
from typing import Tuple

import aiohttp
from aiohttp import ClientConnectorError
from binance import AsyncClient, BinanceSocketManager
from binance.enums import SIDE_SELL, SIDE_BUY, TIME_IN_FORCE_GTC, ORDER_STATUS_NEW, \
    ORDER_STATUS_FILLED, ORDER_STATUS_PARTIALLY_FILLED, ORDER_STATUS_REJECTED, ORDER_STATUS_EXPIRED, \
    ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT, ORDER_TYPE_STOP_LOSS
from binance.exceptions import BinanceAPIException

import global_flags
from TypingClient import TypingClient
from bot_generator import BotGenerator, STOPPED
from conf import STREAM_MSG_TIMEOUT
from shared_time import sleep_speed, get_now
from simulate_client import EndOfDatas, to_str_date
from smart_trades_conf import *
from stream_multiplex import add_multiplex_socket
from stream_user import add_user_socket
from tools import split_symbol, Wallet, atomic_load_json, atomic_save_json, wait_queue_init, generate_order_id, \
    update_order, log_add_order, json_order, str_order, update_wallet, log_order, Order, get_order_price, log_wallet


def _benefice(log: logging, symbol: str, wallet: Dict[str, Decimal], base_solde: Decimal, quote_solde: Decimal) -> None:
    base, quote = split_symbol(symbol)
    log.info(f"###### Result: {wallet[base] - base_solde} {base} / {wallet[quote] - quote_solde} {quote}")


async def _add_order(client: TypingClient,
                     log: logging,
                     order: Order,
                     state: str,
                     ok_state: str,
                     prefix:str=""
                     ) -> Tuple[Order, str]:
    try:
        new_order = await client.create_order(**order)
        # Binance est pauvre dans le cas d'un ordre TAKE_PROFIT
        new_order["side"] = order["side"]
        new_order["type"] = order["type"]
        if 'price' in order:
            new_order["price"] = order["price"]
        if 'limit' in order:
            new_order["limit"] = order["limit"]
        if 'quantity' in order:
            new_order["quantity"] = order["quantity"]
        log_add_order(log, order, prefix)
    except BinanceAPIException as ex:
        if ex.code == -2010:  # Duplicate order sent ?, ignore
            if ex.message == "Duplicate order sent.":
                # Retrouve l'ordre dupliqué.
                orders = await client.get_all_orders(symbol=order["symbol"])
                new_order = next(
                    filter(lambda x: x.get("clientOrderId", "") == order["newClientOrderId"],
                           orders))
                # et continue

            elif ex.message == "Account has insufficient balance for requested action.":
                log.error(f"{ex.message}")
                log_order(order)
                return order, SmartTradeBot.STATE_ERROR
            elif ex.message.startswith("Filter failure:"):
                log.error(f"{ex.message}")
                log_order(order)
                return order, SmartTradeBot.STATE_ERROR
            elif ex.message == "Stop price would trigger immediately.":
                # https://dev.binance.vision/t/order-would-trigger-immediately-error/245/2
                log.error(f"{ex.message}")
                log_order(order)
                return order, SmartTradeBot.STATE_ERROR  # TODO: cancel au market
            elif ex.message == "Filter failure: MAX_NUM_ALGO_ORDERS":
                log.error(f"{ex.message}")
                log_order(order)
                return order, SmartTradeBot.STATE_ERROR
            raise
        else:
            raise

    # C'est bon, il est passé
    log.debug(f'Order \'{new_order["clientOrderId"]}\' created')

    return new_order, ok_state


async def _wait_filled(
        client: TypingClient,
        log: logging,
        wallet: Wallet,
        order: Order,
        state: str,
        states: Tuple[str,  # ADD_ORDER for retry
                      str,  # WAIT_WS
                      str,  # WAIT_POLL
                      str],  # OK
        prefix: str,
        get_msg) -> str:
    state_add, state_ws, state_poll, state_ok = states
    if state == state_ws:
        # Attend en websocket
        try:

            log.debug(f"Wait event order status... ({STREAM_MSG_TIMEOUT}")  # TODO: bascule régulirement (mixte des flux)
            msg = await wait_for(get_msg(), timeout=STREAM_MSG_TIMEOUT)
            # See https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md
            if 'e' in msg and msg['e'] == "error":
                # Web socket in error, try with polling
                return states[1]  # Use polling
            elif msg["_stream"].endswith("@trade") and msg['e'] == "trade":
                log.info(f"p={msg['p']}")

            elif msg["_stream"].endswith("@user") and msg['e'] == "executionReport" and \
                    msg['s'] == order["symbol"] and \
                    msg['i'] == order['orderId'] and \
                    msg['X'] != ORDER_STATUS_NEW:
                log_order(log, order, prefix)
                return state_ok  # Order filled
            elif msg["_stream"].endswith("@ticker") and msg['e'] == "24hrTicker":
                b = Decimal(msg['b'])  # Best bid
                a = Decimal(msg['a'])  # Best ask price
                c = Decimal(msg['l'])  # last price
                # log.info(f"bid={b} ask={a} last={c}")
            elif msg["_stream"].endswith("@user"):
                log.info(f"e={msg['e']}")  # outboundAccountPosition
            elif msg["_stream"].endswith("@bookTicker"):
                pass
            else:
                assert False,"unkown msg type"
        except (asyncio.TimeoutError, Empty) as ex:
            # Periodiquement, essaye en polling
            return state_poll

    elif state == state_poll:
        # log.info("Polling get order status...")
        # Dans tous les cas, vérifie un polling
        log.info("Poll order status...")
        order = await client.get_order(symbol=order['symbol'],
                                       orderId=order['orderId'])
        # See https://binance-docs.github.io/apidocs/spot/en/#public-api-definitions
        if order['status'] == ORDER_STATUS_FILLED:
            update_wallet(wallet, order)
            log_order(log, order)
            return state_ok
        elif order['status'] == ORDER_STATUS_PARTIALLY_FILLED:
            # FIXME: Partially a traiter
            update_wallet(wallet, order)
            log_order(log, order, prefix)
            state = state_ok  # Filled
            return state
        elif order['status'] == ORDER_STATUS_REJECTED:
            log.error(f'Order {order["orderId"]} is rejected')  # FIXME
            return state_ok
        elif order['status'] == ORDER_STATUS_EXPIRED:
            log.warning(f'Order {order["orderId"]} is expired. Retry.')
            return state_add
        elif order['status'] == ORDER_STATUS_NEW:
            return state_ws
            # FIXME: si on sait que le stream est done...
            # await sleep(POOLING_SLEEP)
        else:
            assert False, f"Unknown status {order['status']}"
    return state


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class SmartTradeBot(BotGenerator):
    # self.last_price c'est le prix de la dernière transaction

    POOLING_SLEEP = 2 * sleep_speed()

    STATE_INIT = "init"
    STATE_TRAILING_BUY = "trailing_buy"
    STATE_ACTIVATE_TRAILING_BUY = "activate_trailing_buy"
    STATE_CREATE_BUY_ORDER = "create_buy_order"
    STATE_ADD_ORDER = "add_buy_order"
    STATE_WAIT_BUY_ORDER_FILLED_WS = "wait_order_filled_ws"
    STATE_WAIT_BUY_ORDER_FILLED_POLL = "wait_order_filled_poll"
    STATE_BUY_ORDER_FILLED = "wait_buy_order_filled"
    STATE_BUY_ORDER_EXPIRED = "buy_order_expired"

    STATE_TP_ALONE = "tp_alone"
    STATE_ADD_TP_ORDER = "add_tp_order"
    STATE_WAIT_TP_FILLED_POLL = "wait_tp_order_poll"
    STATE_WAIT_TP_FILLED_WS = "wait_tp_order_ws"

    STATE_SL_ALONE = "sl_alone"
    STATE_ADD_SL_ORDER = "add_sl_order"
    STATE_WAIT_SL_FILLED_POLL = "wait_sl_order_poll"
    STATE_WAIT_SL_FILLED_WS = "wait_sl_order_ws"

    STATE_STOP_LOSS_TRAILING = "st_trailing"
    STATE_ACTIVATE_TRAILING_SL = "active_st_condition"
    STATE_SL_TIMEOUT = "active_st_timeout"
    STATE_STOP_LOSS = "stop_lost"

    STATE_WAIT_TP_OR_SL_ORDER_FILLED_WITH_WEB_SOCKET = "wait_take_profit_or_stop_loss_web_socket"
    STATE_WAIT_TAKE_PROFIT = "wait_take_profit"

    STATE_FINISHED = "finished"
    STATE_CANCELING = "canceling"
    STATE_CANCELED = "canceled"
    STATE_ERROR = "error"

    def is_error(self):
        return self.state == SmartTradeBot.STATE_ERROR

    async def _start(self,
                     client: AsyncClient,
                     agent_queue: Queue,
                     log: logging,
                     init: Dict[str, str],
                     **kwargs) -> 'WinterSummerBot':
        self._generator = self.generator(client,
                                         agent_queue,
                                         log,
                                         init,
                                         **kwargs)
        await self.next()
        return self

    async def generator(self,
                        client: AsyncClient,
                        bot_queue: Queue,
                        log: logging,
                        init: Dict[str, str],  # Initial context
                        socket_manager: BinanceSocketManager,
                        client_account: Dict[str, Any],
                        generator_name: str,
                        agent_queues: Dict[str, Queue],  # All agent queues
                        conf: Dict[str, Any],
                        **kwargs) -> None:
        try:
            if not init:
                # Premier départ
                init = {"state": SmartTradeBot.STATE_INIT,
                        "order_state": None,
                        "wallet": {},
                        "last_price": Decimal(0),
                        "activate_trailing_buy": False,
                        "activate_trailing_take_profit": False,
                        "activate_trailing_stop_loss": False,
                        }
            self.update(init)
            del init

            yield self

            # ---- Initialisation du bot
            continue_if_partially = True  # FIXME

            # Récupération des paramètres
            params = parse_conf(conf)

            # Clean all orders for this symbol
            log.warning(f"Clean all pending orders for {params.symbol}")
            for order in await client.get_open_orders(symbol=params.symbol):
                if order["status"] == ORDER_STATUS_NEW:
                    await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

            # ---- Gestion des queues de communications asynchones
            # Initialisation d'une queue mixte
            # L'enregistrement des streams ne doit être fait qu'au début du traitement
            # Peux recevoir des messages non demandés
            # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
            # FIXME: dynamic add ticker
            mixte_queue = asyncio.Queue()  # Création d'une queue commune

            async def put_mixte(msg: Dict[str, Any]) -> None:
                mixte_queue.put_nowait(msg)

            add_multiplex_socket(params.symbol.lower() + "@trade", put_mixte)
            add_multiplex_socket(params.symbol.lower() + "@ticker", put_mixte)
            add_multiplex_socket(params.symbol.lower() + "@bookTicker", put_mixte)
            add_user_socket(put_mixte)

            def _get_msg():
                return mixte_queue.get()

            # ---- Analyse des paramètres
            # Récupération des contraintes des devices
            symbol_info = await client.get_symbol_info(params.symbol)

            # Récupération des balances du wallet master
            base, quote = split_symbol(params.symbol)
            balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
            balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))
            self.wallet[base] = balance_base["free"]
            self.wallet[quote] = balance_quote["free"]
            # ----------------- Reprise du context après un crash ou un reboot
            if self:
                # Reprise des generateurs pour les ordres en cours
                pass
            else:
                # Premier démarrage
                log.info(f"Started")

            # ----- Synchronisation entre les agents: Attend le démarrage des queues user et multiplex
            wait_init_queue = True  # FIXME
            if wait_init_queue:
                await wait_queue_init(bot_queue)
                wait_init_queue = False

            # Finite state machine
            # C'est ici que le bot travaille sans fin, en sauvant sont état à chaque transition
            while True:
                try:
                    # Reception d'ordres venant de l'API. Par exemple, ajout de fond, arrêt, etc.
                    msg = bot_queue.get_nowait()
                    if msg['e'] == 'kill':
                        log.warning("Receive kill")
                        return
                except QueueEmpty:
                    pass  # Ignore

                if self.state == SmartTradeBot.STATE_INIT:
                    if params.training_buy:
                        if params.training_buy < 0:
                            self.active_buy_condition = params.price * (1 + params.training_buy)
                            self.buy_condition = params.price
                        else:
                            self.active_buy_condition = params.price
                            self.buy_condition = params.price * (1 + params.training_buy)
                        log.info(f"Set trailing buy condition at {self.active_buy_condition} {quote}")
                        self.state = SmartTradeBot.STATE_TRAILING_BUY
                    else:
                        self.state = SmartTradeBot.STATE_CREATE_BUY_ORDER
                    yield self

                elif self.state == SmartTradeBot.STATE_TRAILING_BUY:
                    msg = await mixte_queue.get()
                    if 'e' in msg and msg['e'] == "error":
                        # Web socket in error
                        log.error("Web socket for market in error")
                        await sleep(2)
                        # boucle
                        self.state = SmartTradeBot.STATE_TRAILING_BUY
                        yield self
                    elif msg["_stream"].endswith("@trade") and \
                            msg['e'] == "trade" and msg['s'] == params.symbol:
                        trade_price = Decimal(msg['p'])
                        log.info(f"{trade_price=}")
                        if trade_price <= self.active_buy_condition:
                            self.state = SmartTradeBot.STATE_ACTIVATE_TRAILING_BUY
                            self.buy_condition = trade_price * (1 + params.training_buy)
                            log.info("Activate trailing buy")
                            log.info(f"Buy condition = {self.buy_condition}")
                            self.activate_trailing_buy = True
                            yield self

                elif self.state == SmartTradeBot.STATE_ACTIVATE_TRAILING_BUY:
                    msg = await mixte_queue.get()
                    if msg['e'] == "error":
                        # Web socket in error
                        log.error("Web socket for market in error")
                        await sleep(2)
                        # boucle
                        self.state = SmartTradeBot.SmartTradeBot.STATE_TRAILING_BUY
                        yield self
                    elif msg['e'] == "trade" and msg['s'] == params.symbol:
                        trade_price = Decimal(msg['p'])
                        log.info(f"{trade_price=}")
                        if trade_price > self.buy_condition:
                            log.info(f"Price is correct. Buy at market")
                            self.state = SmartTradeBot.STATE_CREATE_BUY_ORDER  # TODO: market condition
                        else:
                            new_buy_condition = trade_price * (1 + params.training_buy)
                            if new_buy_condition < self.buy_condition:
                                self.buy_condition = new_buy_condition
                                log.info(f"Buy condition = {self.buy_condition}")
                        yield self

                elif self.state == SmartTradeBot.STATE_CREATE_BUY_ORDER:
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
                        quantity = balance_quote * params.size
                        order["quantity"] = quantity
                    elif params.total:
                        order["quoteOrderQty"] = params.total

                    if params.training_buy or params.mode == MARKET:
                        order["type"] = ORDER_TYPE_MARKET
                    elif params.mode == LIMIT:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
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
                    # Valide la création d'un ordre
                    await client.create_test_order(**json_order(str_order(order)))

                    # Mémorise l'ordre pour pouvoir le rejouer si nécessaire
                    self.order = order
                    self.state = SmartTradeBot.STATE_ADD_ORDER
                    yield self

                elif self.state == SmartTradeBot.STATE_ADD_ORDER:
                    # Ajoute un ordre d'achat
                    self.order, self.state = await _add_order(client,
                                                              log,
                                                              self.order,
                                                              self.state,
                                                              SmartTradeBot.STATE_WAIT_BUY_ORDER_FILLED_WS)
                    yield self


                elif self.state in (SmartTradeBot.STATE_WAIT_BUY_ORDER_FILLED_WS,
                                    SmartTradeBot.STATE_WAIT_BUY_ORDER_FILLED_POLL):
                    # Confirmation de l'ordre en web_socket et polling
                    self.state = await _wait_filled(client,
                                                    log,
                                                    self.wallet,
                                                    self.order,
                                                    self.state,
                                                    (SmartTradeBot.STATE_ADD_ORDER,
                                                     SmartTradeBot.STATE_WAIT_BUY_ORDER_FILLED_WS,
                                                     SmartTradeBot.STATE_WAIT_BUY_ORDER_FILLED_POLL,
                                                     SmartTradeBot.STATE_BUY_ORDER_FILLED),
                                                    "****** ",
                                                    _get_msg)
                    yield self


                # ---------------- Aiguillage suivant les cas
                elif self.state == SmartTradeBot.STATE_BUY_ORDER_FILLED:
                    if self.order["status"] not in [ORDER_STATUS_FILLED, ORDER_STATUS_PARTIALLY_FILLED]:
                        self.state = SmartTradeBot.STATE_ERROR
                        yield self
                    elif not params.use_take_profit and not params.use_stop_loss:
                        # Rien à faire après l'achat
                        self.state = SmartTradeBot.STATE_FINISHED
                    else:
                        if params.use_take_profit and not params.take_profit_trailing \
                                and not params.use_stop_loss \
                                and params.take_profit_base == "last":  # TODO: si params.take_profit_base != "last", à la main
                            # TP sans SL ni trailing, sur une base 'last'
                            self.state = SmartTradeBot.STATE_TP_ALONE
                            yield self
                        elif params.use_stop_loss and not params.use_take_profit and not params.stop_loss_trailing and params.use_stop_loss:
                            # SL sans TP ni trailing, sur une base 'last'
                            self.state = SmartTradeBot.STATE_SL_ALONE
                            yield self
                        elif params.use_stop_loss and params.stop_loss_trailing:
                            percent = params.stop_loss_percent
                            if params.stop_loss_limit:
                                percent = get_order_price(self.order) / params.stop_loss_limit
                            self.stop_loss_percent = percent
                            self.active_stop_loss_condition = get_order_price(self.order) * (1 + percent)
                            self.active_stop_loss_timeout = None
                            log.info(f"Set trailing stop-lost condition at {self.active_stop_loss_condition}")
                            self.state = SmartTradeBot.STATE_STOP_LOSS_TRAILING
                            yield self
                        else:
                            assert False, "situation à gérer"

                # ---------- Gestion d'un ordre TP seul
                elif self.state == SmartTradeBot.STATE_TP_ALONE:
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
                                price = get_order_price(self.order) * (1 + params.take_profit_limit_percent)
                            else:
                                price = params.take_profit_limit
                            order["price"] = price
                            order["stopPrice"] = price
                            order["quantity"] = Decimal(self.order["executedQty"])
                        else:
                            assert False, "Trouve une alternative"
                    elif params.take_profit_mode == LIMIT:
                        order["type"] = ORDER_TYPE_TAKE_PROFIT_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        # FIXME: take_profit_limit_percent
                        if params.take_profit_limit_percent:
                            price = get_order_price(self.order) * (1 + params.take_profit_limit_percent)
                        else:
                            price = params.take_profit_limit
                        order["price"] = price
                        order["stopPrice"] = params.take_profit_limit
                        order["quantity"] = Decimal(self.order["executedQty"])
                    else:
                        assert False, f"Invalid {params.take_profit_limit}"
                    order = update_order(symbol_info, None, order)
                    await client.create_test_order(**json_order(str_order(order)))
                    self.take_profit_order = order
                    self.state = SmartTradeBot.STATE_ADD_TP_ORDER
                    yield self

                elif self.state == SmartTradeBot.STATE_ADD_TP_ORDER:
                    self.take_profit_order, self.state = await _add_order(client,
                                                                          log,
                                                                          self.take_profit_order,
                                                                          self.state,
                                                                          SmartTradeBot.STATE_WAIT_TP_FILLED_WS)
                    yield self

                elif self.state in (SmartTradeBot.STATE_WAIT_TP_FILLED_POLL, SmartTradeBot.STATE_WAIT_TP_FILLED_WS):
                    # Confirmation de l'ordre en web_socket et polling
                    self.state = await _wait_filled(client,
                                                    log,
                                                    self.wallet,
                                                    self.take_profit_order,
                                                    self.state,
                                                    (SmartTradeBot.STATE_ADD_TP_ORDER,
                                                     SmartTradeBot.STATE_WAIT_TP_FILLED_POLL,
                                                     SmartTradeBot.STATE_WAIT_TP_FILLED_WS,
                                                     SmartTradeBot.STATE_FINISHED),
                                                    "*** TAKE PROFIT:",
                                                    _get_msg)
                    yield self


                # ---------- Gestion d'un ordre SL seul
                elif self.state == SmartTradeBot.STATE_SL_ALONE:
                    # Stop lost fixe
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                    }
                    if params.stop_loss_mode == MARKET:
                        if params.stop_loss_base == "last":
                            if ORDER_TYPE_STOP_LOSS_LIMIT in symbol_info.orderTypes:
                                order["type"] = ORDER_TYPE_STOP_LOSS_LIMIT
                                order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                                if params.stop_loss_limit_percent:
                                    stop_price = get_order_price(self.order) * (1 + params.stop_loss_limit_percent)
                                else:
                                    stop_price = params.stop_loss_limit
                                order["price"] = stop_price  # TODO: trouver explication
                                order["stopPrice"] = stop_price
                                order["quantity"] = Decimal(self.order["executedQty"])
                            else:
                                assert False, "Trouve une alternative"
                        else:
                            # TODO: si params.stop_loss_base != "last", à la main
                            assert False,"trouver une alernative"
                    elif params.stop_loss_mode == COND_LIMIT_ORDER:  # FIXME: c'est quoi le "mode" ? C'est le Tracking method selection,
                        if params.stop_loss_base == "last":
                            order["type"] = ORDER_TYPE_STOP_LOSS_LIMIT
                            order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                            if params.stop_loss_limit_percent:
                                stop_price = get_order_price(self.order) * (1 + params.stop_loss_limit_percent)
                            else:
                                stop_price = params.stop_loss_limit
                            order["price"] = stop_price  # TODO: trouver explication
                            order["stopPrice"] = stop_price
                            order["quantity"] = Decimal(self.order["executedQty"])
                        else:
                            # TODO: si params.stop_loss_base != "last", à la main
                            assert False, "Trouve une alternative"
                    else:
                        assert False, f"Invalid {params.stop_loss_mode}"
                    order = update_order(symbol_info, None, order)
                    await client.create_test_order(**json_order(str_order(order)))
                    self.stop_loss_order = order
                    self.state = SmartTradeBot.STATE_ADD_SL_ORDER
                    yield self

                elif self.state == SmartTradeBot.STATE_ADD_SL_ORDER:
                    self.stop_loss_order, self.state = await _add_order(client,
                                                                        log,
                                                                        self.stop_loss_order,
                                                                        self.state,
                                                                        SmartTradeBot.STATE_WAIT_SL_FILLED_POLL,
                                                                        "STOP LOSS: Try to ")
                    yield self

                elif self.state in (SmartTradeBot.STATE_WAIT_SL_FILLED_POLL, SmartTradeBot.STATE_WAIT_SL_FILLED_WS):
                    # Confirmation de l'ordre en web_socket et polling
                    self.state = await _wait_filled(client,
                                                    log,
                                                    self.wallet,
                                                    self.stop_loss_order,
                                                    self.state,
                                                    (
                                                        SmartTradeBot.STATE_ADD_SL_ORDER,
                                                        SmartTradeBot.STATE_WAIT_SL_FILLED_POLL,
                                                        SmartTradeBot.STATE_WAIT_SL_FILLED_WS,
                                                        SmartTradeBot.STATE_FINISHED),
                                                    "****** STOP LOSS:",
                                                    _get_msg)  # FIXME: finished ?
                    yield self


                # ---------------- SL trailing
                elif self.state == SmartTradeBot.STATE_STOP_LOSS_TRAILING:
                    msg = await mixte_queue.get()
                    if 'e' in msg and msg['e'] == "error":
                        # Web socket in error
                        log.error("Web socket for market in error")
                        await sleep(2)  # FIXME
                        # boucle
                        self.state = SmartTradeBot.STATE_TRAILING_BUY
                        yield self
                    elif msg["_stream"].endswith("@trade") and \
                            msg['e'] == "trade" and msg['s'] == params.symbol:
                        trade_price = Decimal(msg['p'])
                        log.info(f"{trade_price=}")
                        if trade_price < self.active_stop_loss_condition:
                            now = get_now()
                            if self.active_stop_loss_timeout:
                                if self.active_stop_loss_timeout + params.stop_loss_timeout > now:
                                    log.warning("Activate STOP-LOSS...")
                                    self.state = SmartTradeBot.STATE_STOP_LOSS
                                    yield self
                            else:
                                self.active_stop_loss_timeout=get_now()
                                log.debug(f"Stop-lost start timer... {to_str_date(self.active_stop_loss_timeout)}")
                                yield self
                        else:
                            if self.active_stop_loss_timeout:
                                log.debug("Deactivate stop-lost timeout")
                                self.active_stop_loss_timeout = None
                            new_stop_loss_condition = trade_price * (1 + self.stop_loss_percent)
                            if new_stop_loss_condition > self.active_stop_loss_condition:
                                self.active_stop_loss_condition = new_stop_loss_condition
                                log.info(f"Update stop lost condition = {self.active_stop_loss_condition}")

                elif self.state == SmartTradeBot.STATE_STOP_LOSS:
                    # Add trade to stop loss
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "type": ORDER_TYPE_MARKET,
                        "symbol": params.symbol,
                        "side": SIDE_SELL,
                        "quantity": Decimal(self.order["executedQty"])
                    }
                    if params.stop_loss_order_price:
                        order["type"] = ORDER_TYPE_LIMIT
                        order["timeInForce"] = TIME_IN_FORCE_GTC  # TODO: paramétrable ?
                        order["price"] = params.stop_loss_order_price
                    else:
                        order["type"] = ORDER_TYPE_MARKET
                    order = update_order(symbol_info, None, order)
                    # No time to test
                    # await client.create_test_order(**json_order(str_order(order)))
                    self.stop_loss_order = order
                    self.state = SmartTradeBot.STATE_ADD_SL_ORDER
                    yield self

                # ---------------- End
                elif self.state == SmartTradeBot.STATE_FINISHED:
                    log.info("Smart Trade finished")
                    return
                # ---------------- Cancel and error
                elif self.state == SmartTradeBot.STATE_CANCELING:
                    try:
                        await client.cancel_order(symbol=self.order['symbol'], orderId=self.order['orderId'])
                    except BinanceAPIException as ex:
                        if ex.code == -2011 and ex.message == "Unknown order sent.":
                            pass  # Ignore
                        else:
                            raise
                    self.state = SmartTradeBot.STATE_CANCELED
                    yield self
                elif self.state == SmartTradeBot.STATE_CANCELED:
                    return
                elif self.state == SmartTradeBot.STATE_ERROR:
                    log.error("State error")
                    return
                else:
                    log.error(f'Unknown state \'{self["state"]}\'')
                    self.state = SmartTradeBot.STATE_ERROR
                    yield self
                    return


        except BinanceAPIException as ex:
            if ex.code in (-1013, -2010) and ex.message.startswith("Filter failure:"):
                log.error(ex.message)
                self.state = SmartTradeBot.STATE_ERROR
                yield self
            elif ex.code == -2011 and ex.message == "Unknown order sent.":
                log.error(ex.message)
                log.error(self.order)
                log.exception(ex)
                self.state = SmartTradeBot.STATE_ERROR
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request is outside of the recvWindow.':
                log.error(ex.message)
                # self.state = SmartTradeBot.STATE_ERROR
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request was 1000ms ahead of the server\'s time.':
                log.error(ex.message)
                # self.state = SmartTradeBot.STATE_ERROR
                yield self
            else:
                log.exception("Unknown error")
                log.exception(ex)
                self.state = SmartTradeBot.STATE_ERROR
                yield self


        except (ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
            self.state = SmartTradeBot.STATE_ERROR
            # Attention, pas de sauvegarde.
            raise


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: AsyncClient,
              socket_manager: BinanceSocketManager,
              client_account: Dict[str, Any],
              bot_name: str,
              agent_queues: Dict[str, Queue],
              conf: Dict[str, str]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = agent_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    json_generator = {}
    if not global_flags.simulation and path.exists():
        json_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={json_generator['state']}")
    # Puis initialisatio du generateur
    bot_generator = await SmartTradeBot.create(client,
                                               bot_queue,
                                               log,
                                               json_generator,
                                               socket_manager=socket_manager,
                                               generator_name=bot_name,
                                               client_account=client_account,
                                               agent_queues=agent_queues,
                                               conf=conf,
                                               )
    try:
        while True:
            if await bot_generator.next() == STOPPED:
                break
            if not global_flags.simulation:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                # FIXME atomic_save_json(bot_generator, path)
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.wallet)
        raise
