"""
Cette stratégie cherche a exploiter les grands écarts sur les cryptos, en IAant du fait qu'il y a de temps
en temps des fortes chutes de X%, puis que le cours fini par dépasser le top.

L'idée est de capturer les chutes fortes, pour entrer sur le marché. Puis de revendre au dépassement du top.
Parametres:
{
      "symbol": "BNBBUSD",  # La paire à trader
      "base_quantity": "0.1%", # La quantité de 'base' à utiliser (ou zéro)
      "quote_quantity": "0.1%", # La quantité de 'quote' à utiliser (ou zéro mais l'un des deux)
      "interval": "1w" # Moyenne entre top et bottom de la période précédente comme point de d'analyse
      "winter": "-30%", # Si le marché tombe de 'winter' % sur la période, alors achète
      "summer": "2%" # Si le marché remonte au dessus du top, de 'summer' %, alors vend
}

Au début, aligne les 2 coins pour n'en avoir qu'un seul, suivant le plus gros.
Ce n'est pas toujours possible, à cause des limites de ventes et d'achats de la paire.
Pour le moment, cela génère une erreur, avant de trouver mieux.
"""

# TODO: client.get_lending_product_list() pour stacker en attendant, en été
from asyncio import QueueEmpty
from datetime import timezone
from pathlib import Path

import aiohttp
from aiohttp import ClientConnectorError
from binance import BinanceSocketManager
from binance.enums import *
# Mémorise l'état de l'automate, pour permettre une reprise à froid
from binance.helpers import *

from add_order import *
from bot_generator import STOPPED
from multiplex_stream import add_multiplex_socket
from tools import atomic_load_json, generate_order_id, wait_queue_init, update_order, split_symbol, \
    atomic_save_json, check_order, log_order, json_order, log_add_order, to_usdt, str_order, str_d


# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60

def _benefice(log: logging, symbol: str, wallet: Dict[str, Decimal], base_solde: Decimal, quote_solde: Decimal) -> None:
    base, quote = split_symbol(symbol)
    log.info(f"###### Result: {str_d(wallet[base] - base_solde)} {base} / {str_d(wallet[quote] - quote_solde)} {quote}")


MINIMUM_REDUCE_PRICE = Decimal("0.99")


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class WinterSummerBot(BotGenerator):
    POOLING_SLEEP = 2

    STATE_INIT = "init"
    STATE_ALIGN_WINTER = "align_winter"
    STATE_WAIT_ALIGN_WINTER_ORDER_FILLED = "wait_align_winter_filled"
    STATE_WAIT_ALIGN_WINTER_LOOP = "wait_align_winter_loop"
    STATE_ALIGN_SUMMER = "align_summer"
    STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED = "wait_align_summer_filled"
    STATE_WINTER_ORDER = "winter_order"
    STATE_WAIT_WINTER_ORDER_FILLED = "wait_winter_order_filled"
    STATE_WAIT_WINTER_CANCEL_ORDER = "wait_winter_cancel_order"
    STATE_WAIT_SUMMER = "wait_summer"
    STATE_WAIT_SUMMER_ORDER_FILLED = "wait_summer_order_filled"

    STATE_ERROR = "error"

    async def _start(self,
                     client: AsyncClient,
                     agent_queue: Queue,
                     log: logging,
                     init: Dict[str, Any],
                     **kwargs) -> 'WinterSummerBot':
        self._generator = self.generator(client,
                                         agent_queue,
                                         log,
                                         init,
                                         **kwargs)
        # # Réactive les générateurs pour les ordres
        # if 'winter_order' in init:
        #     init['winter_order'] = await AddOrder.create(client,user_queue,log,init['winter_order'])
        # if 'summer_order' in init:
        #     init['summer_order'] = await AddOrder.create(client,user_queue,log,init['summer_order'])
        await self.next()
        return self

    async def generator(self,
                        client: AsyncClient,
                        bot_queue: Queue,
                        log: logging,
                        init: Dict[str, Any],  # Initial context
                        socket_manager: BinanceSocketManager,
                        client_account: Dict[str, Any],
                        generator_name: str,
                        agent_queues: Dict[str, Queue],  # All agent queues
                        conf: Dict[str, Any]) -> None:
        try:
            if not init:
                # Premier départ
                init = {"state": WinterSummerBot.STATE_INIT,
                        "order_state": None,
                        "wallet": {},
                        "last_price": Decimal(0)
                        }
            self.update(init)
            del init

            yield self
            user_bot = agent_queues["user_stream"]
            user_queue = Queue()  # Création d'une queue pour les streams 'user'
            # TODO: capture des exceptions globale pour alerte
            wait_init_queue = True
            continue_if_partially = False

            # ---- Initialisation du bot

            # Récupération des paramètres
            symbol = conf["symbol"]
            lower_percent = Decimal(conf['winter'].strip('%')) / 100
            upper_percent = Decimal(conf['summer'].strip('%')) / 100
            interval = conf.get("interval", KLINE_INTERVAL_1WEEK)
            interval_top = conf.get("interval_top", KLINE_INTERVAL_1MONTH)
            history_top = conf.get("history_top", "5 years ago UTC")
            if 'top' in conf:
                self.top_value = Decimal(conf["top"])  # Top fixé
            else:
                self.top_value = await self.get_top_value(client, symbol, history_top, interval_top)

            log.info(f"Start with top={self.top_value}")

            # ---- Gestion des queues de communications asynchones

            # L'enregistrement des streams ne doit être fait qu'au début du traitement
            # Peux recevoir des messages non demandés
            # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
            market_queue = asyncio.Queue()  # Queue to receive event from market

            async def event(msg: Dict[str, Any]) -> None:
                market_queue.put_nowait(msg)

            # add_multiplex_socket(symbol.lower()+"@ticker", event)
            add_multiplex_socket(symbol.lower() + "@aggTrade", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@trade", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@kline_1m", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@miniTicker", event)  # 1m

            # ---- Analyse des paramètres
            # Calcul de la cible top pour vendre
            self.target_summer = self.top_value * (1 + upper_percent)

            # Récupération des contraintes des devices
            symbol_info = await client.get_symbol_info(symbol)

            # Récupération des balances du wallet master
            base, quote = split_symbol(symbol)
            balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
            balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))

            # ----------------- Reprise du context après un crash ou un reboot
            if self:
                # Reprise des generateurs pour les ordres en cours
                if 'winter_order' in self:
                    self.winter_order = await AddOrder.create(client,
                                                              user_queue,
                                                              log,
                                                              self.winter_order,
                                                              order=self.winter_order['order'],
                                                              wallet=self.wallet,
                                                              continue_if_partially=continue_if_partially)
                if 'summer_order' in self:
                    self.summer_order = await AddOrder.create(client,
                                                              user_queue,
                                                              log,
                                                              self.summer_order,
                                                              order=self.summer_order['order'],
                                                              wallet=self.wallet,
                                                              continue_if_partially=continue_if_partially)
            else:
                # Premier démarrage
                log.info(f"Started")

            # FIXME Clean all new order for symbol
            # log.warning("Cancel all orders !")
            # for order in await client.get_open_orders(symbol=symbol):
            #     if order["status"] == ORDER_STATUS_NEW:
            #         await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

            # List open order
            # open_orders=await client.get_open_orders(symbol='BNBBTC')
            # for order in open_orders:
            #     log.info(f'Open order {order["orderId"]}')

            # ----- Synchronisation entre les agents: Attend le démarrage des queues user et multiplex
            if wait_init_queue:
                await wait_queue_init(bot_queue)
                wait_init_queue = False





            # Finite state machine
            # C'est ici que le bot travaille sans fin, en sauvant sont état à chaque transition
            while True:
                try:
                    # Reception d'ordres venant de l'API. Par exemple, ajout de fond, arrêt, etc.
                    msg = user_queue.get_nowait()
                    if msg['msg'] == 'kill':
                        log.warning("Receive kill")
                        return
                except QueueEmpty:
                    pass  # Ignore

                if self.state == WinterSummerBot.STATE_INIT:
                    # Récupère le volume de token à partir des balances free
                    # puis convertie en USDT pour savoir lequel est le plus gros
                    # Cela permet de savoir la saison. L'autre devise est acheté ou vendu pour
                    # avoir l'un des soldes à zéro.
                    quote_quantity = conf.get('quote_quantity', "0")
                    if '%' in quote_quantity:
                        self.quote_quantity = Decimal(balance_quote['free'] * Decimal(
                            quote_quantity.strip('%')) / 100)
                    else:
                        self.quote_quantity = Decimal(quote_quantity)

                    base_quantity = conf.get('base_quantity', "0")
                    if '%' in base_quantity:
                        self.base_quantity = Decimal(
                            balance_base['free'] * Decimal(base_quantity.strip('%')) / 100)
                    else:
                        self.base_quantity = Decimal(base_quantity)

                    self.wallet[base] = self.base_quantity
                    self.wallet[quote] = self.quote_quantity

                    base_usdt = await to_usdt(client, log, base, self.wallet[base])
                    quote_usdt = await to_usdt(client, log, quote, self.wallet[quote])

                    log.info(f"Start with {str_d(self.base_quantity)} {base} "
                             f"(${str_d(base_usdt)}) and "
                             f"{str_d(self.quote_quantity)} {quote} (${str_d(quote_usdt)})")
                    if base_usdt < quote_usdt:
                        # On est en ete
                        self.state = WinterSummerBot.STATE_WINTER_ORDER if not base_usdt else WinterSummerBot.STATE_ALIGN_WINTER
                        log.info("Start in winter")
                    else:
                        self.state = WinterSummerBot.STATE_WAIT_SUMMER if not quote_usdt else WinterSummerBot.STATE_ALIGN_SUMMER
                        log.info("Start in summer")
                    yield self

                # Vend les 'base' pour n'avoir que du 'quote'
                elif self.state == WinterSummerBot.STATE_ALIGN_WINTER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    # quantity = self.wallet[quote]
                    # updated_quantity = update_market_lot_size(symbol_info, quantity)
                    # if True : # quantity != updated_quantity:
                    #     log.info("Must sell by lot")
                    #     order = {
                    #         "newClientOrderId": generate_order_id(generator_name),
                    #         "symbol": symbol,
                    #         "side": SIDE_SELL,
                    #         "type": ORDER_TYPE_MARKET,
                    #         "quantity": updated_quantity,
                    #     }
                    #     order = update_order(symbol_info, current_price, order)
                    #     log_order(log, order)
                    #     await client.create_test_order(**json_order(str_order(order)))
                    #     self.winter_order = await AddOrder.create(
                    #         client,
                    #         user_queue,
                    #         log,
                    #         order=order,
                    #         wallet=self.wallet,
                    #     )
                    #     self.state = WinterSummerBot.STATE_WAIT_ALIGN_WINTER_LOOP
                    #     yield self
                    # else:
                    if True:  # Vend toujours la qty dispo, sinon erreur
                        # TODO: Voir orderTypes[...] pour vérifier ce qui est possible
                        order = {
                            "newClientOrderId": generate_order_id(generator_name),
                            "symbol": symbol,
                            "side": SIDE_SELL,
                            "type": ORDER_TYPE_MARKET,
                            "quoteOrderQty": self.wallet[base],
                        }
                        order = update_order(symbol_info, current_price, order)
                        log_add_order(log, order, prefix="For align, try to ")
                        await client.create_test_order(**json_order(str_order(order)))
                        self.winter_order = await AddOrder.create(
                            client,
                            user_queue,
                            log,
                            order=order,
                            wallet=self.wallet,
                            continue_if_partially=continue_if_partially
                        )
                        self.state = WinterSummerBot.STATE_WAIT_ALIGN_WINTER_ORDER_FILLED
                        yield self
                # TODO: Essaie d'achat successif par lot, pour dépasser les limites markets.
                # elif self.state == WinterSummerBot.STATE_WAIT_ALIGN_WINTER_LOOP:
                #     # N'arrive pas à tout vendre d'un coup, donc doit le faire petit à petit
                #     await self.winter_order.next()
                #     if self.winter_order.is_filled():
                #         del self.winter_order
                #         self.state = WinterSummerBot.STATE_WINTER_ORDER
                #         log.info(f"{self.wallet=}")
                #         log.info("Wait the summer...")
                #         yield self
                #     elif self.winter_order.is_error():
                #         # FIXME
                #         self.state == WinterSummerBot.STATE_ERROR
                #         yield self
                elif self.state == WinterSummerBot.STATE_WAIT_ALIGN_WINTER_ORDER_FILLED:
                    await self.winter_order.next()
                    if self.winter_order.is_filled():
                        del self.winter_order
                        self.state = WinterSummerBot.STATE_ALIGN_WINTER
                        log.info(f"{self.wallet=}")
                        log.info("Loop...")
                        yield self
                    elif self.winter_order.is_error():
                        # FIXME
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                # Vend les 'quote' pour n'avoir que du 'base'
                elif self.state == WinterSummerBot.STATE_ALIGN_SUMMER:
                    current_price = Decimal((await client.get_symbol_ticker(symbol=symbol))["price"])
                    order = {
                        "newClientOrderId": generate_order_id(generator_name),
                        "symbol": symbol,
                        "side": SIDE_BUY,
                        "type": ORDER_TYPE_MARKET,
                        "quoteOrderQty": self.wallet[quote],
                    }
                    order = update_order(symbol_info, current_price, order)  # FIXME
                    # order['price'] = Decimal("1528.93")
                    # order['quantity'] = round_step_size(order['quantity'], symbol_info.lot.stepSize)
                    log_add_order(log, order, prefix="For align, try to ")
                    await client.create_test_order(**json_order(str_order(order)))
                    self.summer_order = await AddOrder.create(
                        client,
                        user_queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=continue_if_partially
                    )
                    self.state = WinterSummerBot.STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED
                    yield self

                elif self.state == WinterSummerBot.STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED:
                    await self.summer_order.next()
                    if self.summer_order.is_filled():
                        log_order(log, self.summer_order.order)
                        del self.summer_order
                        self.state = WinterSummerBot.STATE_WINTER_ORDER
                        log.info(f"{self.wallet=}")
                        log.info(
                            f"Wait the winters... ({self.wallet[base]:f} {base} / {self.wallet[quote]:f} {quote})")
                        yield self
                    elif self.summer_order.is_error():
                        # FIXME
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                elif self.state == WinterSummerBot.STATE_WINTER_ORDER:
                    # Calcul la moyenne de la période précédente, et ajuste un ordre
                    # sur cette base.
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    # log.info(f"{current_price=}")
                    old_avg_price = self.avg_price if 'avg_price' in self else Decimal(0)
                    self.avg_price = await self.get_avg_last_interval(client, interval, symbol)
                    log.info(f">>> new avg_price = {self.avg_price} for {interval} ({old_avg_price=})")
                    price = self.avg_price * (1 + lower_percent)
                    log.info(f"Je cherche à acheter lorsque le prix est < à avg_price de {lower_percent}")
                    if price > self.last_price:
                        log.warning(
                            f"Official lower BUY price is upper previous SELL price "
                            f"({str_d(price)}/{str_d(self.last_price)}). "
                            f"Change to {MINIMUM_REDUCE_PRICE} du last {self.last_price * MINIMUM_REDUCE_PRICE}")
                        price = self.last_price * MINIMUM_REDUCE_PRICE

                    if price > current_price:
                        # FIXME peut etre viré et vendre au MARKET dans ce cas.
                        log.warning(
                            f"Official lower BUY price is upper current price (cur:{price}). "
                            f"Change to {current_price * MINIMUM_REDUCE_PRICE}")
                        price = current_price * MINIMUM_REDUCE_PRICE
                    quantity = self.wallet[quote] / price
                    self.last_price = price

                    # TODO: voir si le type d'ordre n'est pas take profit ou ...
                    # TODO: vérifier si stop_lost est possible pour cette devise, sinon, simple ORDER_TYPE_LIMIT
                    # ['LIMIT', 'LIMIT_MAKER', 'MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']
                    # Choix du type d'ordre, par priorité
                    # if ORDER_TYPE_STOP_LOSS in symbol_info.orderTypes:
                    #     # Achete au marché si touche la limite basse
                    #     order = {
                    #         "newClientOrderId": generate_order_id(generator_name),
                    #         "symbol": symbol,
                    #         "side": SIDE_BUY,
                    #         "type": ORDER_TYPE_STOP_LOSS,
                    #         "timeInForce": TIME_IN_FORCE_GTC,
                    #         "quantity": quantity,
                    #         "stopPrice": price,
                    #     }
                    # elif ORDER_TYPE_STOP_LOSS_LIMIT in symbol_info.orderTypes:
                    #     # Achete au marché si touche la limite basse
                    #     order = {
                    #         "newClientOrderId": generate_order_id(generator_name),
                    #         "symbol": symbol,
                    #         "side": SIDE_BUY,
                    #         "type": ORDER_TYPE_STOP_LOSS_LIMIT,
                    #         "quantity": quantity,
                    #         "timeInForce": TIME_IN_FORCE_GTC,
                    #         "stopPrice": price * Decimal(0.95),  # Ajout 1% pour exposer l'ordre
                    #         "price": price
                    #     }
                    # elif ORDER_TYPE_LIMIT in symbol_info.orderTypes:
                    if True:
                        order = {
                            "newClientOrderId": generate_order_id(generator_name),
                            "symbol": symbol,
                            "side": SIDE_BUY,
                            "type": ORDER_TYPE_LIMIT,
                            # "stopPrice": price,  # Peux être améliorer en faisant un traking de la descente
                            "timeInForce": TIME_IN_FORCE_GTC,
                            "quantity": quantity,
                            "price": price,
                        }
                    else:
                        log.error("Impossible to create a BUY order")
                        self.state = WinterSummerBot.STATE_ERROR
                        yield self
                        continue
                    delta = interval_to_milliseconds(interval) / 4
                    self.next_refresh = datetime.now(timezone.utc).timestamp() * 1000 + delta
                    order = update_order(symbol_info, current_price, order, accept_upper=False)
                    await client.create_test_order(**json_order(str_order(order)))
                    self.winter_order = await AddOrder.create(
                        client,
                        user_queue,
                        log,
                        order=order,
                        wallet=self.wallet,
                        continue_if_partially=continue_if_partially
                    )
                    log_add_order(log, order)
                    self.state = WinterSummerBot.STATE_WAIT_WINTER_ORDER_FILLED
                    yield self


                elif self.state == WinterSummerBot.STATE_WAIT_WINTER_CANCEL_ORDER:
                    await self.winter_order.next()
                    if self.winter_order.is_canceled():
                        log.info(f"Order {self.winter_order.order['clientOrderId']} canceled")
                        self.state = WinterSummerBot.STATE_WINTER_ORDER
                        del self.winter_order
                        yield self

                elif self.state == WinterSummerBot.STATE_WAIT_WINTER_ORDER_FILLED:
                    await self.winter_order.next()
                    if self.winter_order.is_waiting():
                        if datetime.now(timezone.utc).timestamp() * 1000 > self.next_refresh:
                            avg_price = await self.get_avg_last_interval(client, interval, symbol)
                            if avg_price != self.avg_price:
                                log.info(f"modify avg_price = {avg_price}")
                                self.winter_order.cancel()
                                self.state = WinterSummerBot.STATE_WAIT_WINTER_CANCEL_ORDER
                                yield self
                            # else:
                            #     log.debug("same price, continue with the current trade")
                            continue

                    if self.winter_order.is_filled():
                        _benefice(log, symbol, self.wallet, self.base_quantity, self.quote_quantity)
                        del self.winter_order
                        self.state = WinterSummerBot.STATE_WAIT_SUMMER
                        log.info(f"{self.wallet=}")
                        self.top_value = await self.get_top_value(client, symbol, history_top, interval_top)
                        log.info(f"new top_value={self.top_value}")
                        log.info("Wait the summer...")
                        yield self
                    # TODO: si ordre expirer, le relancer
                    elif self.winter_order.is_error():
                        # FIXME: L'ordre est en erreur. Quoi faire ?
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                elif self.state == WinterSummerBot.STATE_WAIT_SUMMER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    log.info(f"current price ={current_price}")
                    price = self.target_summer
                    if price < self.last_price:
                        log.warning(f"Official SELL price below BUY price {price}. Change to {self.last_price}")
                        price = self.last_price
                    self.last_price = price

                    quantity = self.wallet[base]  # On vent tout
                    if not quantity:
                        log.error("Quantity is zero !")
                        self.state = WinterSummerBot.STATE_ERROR
                        yield self
                    else:
                        # TODO: ORO ordre ? Stop loss avec ?
                        # Choix du type d'ordre, par priorité
                        # if ORDER_TYPE_TAKE_PROFIT_LIMIT in symbol_info.orderTypes:
                        #     order = {
                        #         "newClientOrderId": generate_order_id(generator_name),
                        #         "symbol": symbol,
                        #         "side": SIDE_SELL,
                        #         "type": ORDER_TYPE_TAKE_PROFIT_LIMIT,  # A améliorer avec un tracking vers le haut
                        #         "timeInForce": TIME_IN_FORCE_GTC,
                        #         "quantity": quantity,
                        #         "price": price,
                        #         "stopPrice": price * Decimal(0.99),  # En dessous de 1% de l'objectif, active
                        #     }
                        # elif ORDER_TYPE_TAKE_PROFIT in symbol_info.orderTypes:
                        #     order = {
                        #         "newClientOrderId": generate_order_id(generator_name),
                        #         "symbol": symbol,
                        #         "side": SIDE_SELL,
                        #         "type": ORDER_TYPE_TAKE_PROFIT,  # A améliorer avec un tracking vers le haut
                        #         "quantity": quantity,
                        #         "stopPrice": price,
                        #     }
                        # elif ORDER_TYPE_LIMIT in symbol_info.orderTypes:
                        if True:
                            order = {
                                "newClientOrderId": generate_order_id(generator_name),
                                "symbol": symbol,
                                "side": SIDE_SELL,
                                "type": ORDER_TYPE_LIMIT,  # A améliorer avec un tracking vers le haut
                                "timeInForce": TIME_IN_FORCE_GTC,
                                "quantity": quantity,
                                "price": price,
                            }
                        else:
                            log.error("Impossible to create a SELL order")
                            self.state = WinterSummerBot.STATE_ERROR
                            yield self
                            continue
                        order = update_order(symbol_info, current_price, order)
                        check_order(symbol_info, current_price, order)
                        await client.create_test_order(**json_order(str_order(order)))
                        self.summer_order = await AddOrder.create(
                            client,
                            user_queue,
                            log,
                            order=order,
                            wallet=self.wallet,
                            continue_if_partially=continue_if_partially
                        )
                        log_add_order(log, order)
                        self.state = WinterSummerBot.STATE_WAIT_SUMMER_ORDER_FILLED
                        yield self

                elif self.state == WinterSummerBot.STATE_WAIT_SUMMER_ORDER_FILLED:
                    await self.summer_order.next()
                    if self.summer_order.is_filled():
                        _benefice(log, symbol, self.wallet, self.base_quantity, self.quote_quantity)
                        log.info(f"{self.wallet=}")
                        del self.summer_order
                        self.state = WinterSummerBot.STATE_WINTER_ORDER
                        log.info("Wait the winter...")
                        yield self
                    elif self.summer_order.is_error():
                        # FIXME
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self
                    # TODO: wait la remote au dela du top
                elif self.state == WinterSummerBot.STATE_ERROR:
                    log.error("State error")
                    return

        except BinanceAPIException as ex:
            if ex.code in (-1013, -2010) and ex.message.startswith("Filter failure:"):
                # Trading impossible. (filtre non valide.) # TODO
                log.error(ex.message)
                self.state = WinterSummerBot.STATE_ERROR
                yield self
            elif ex.code == -2011 and ex.message == "Unknown order sent.":
                log.error(ex.message)
                log.exception(ex)
                self.state = WinterSummerBot.STATE_ERROR
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request is outside of the recvWindow.':
                log.error(ex.message)
                self.state = WinterSummerBot.STATE_ERROR
                yield self
            else:
                log.exception("Unknown error")
                log.exception(ex)
                self.state = WinterSummerBot.STATE_ERROR
                yield self


        except (ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
            self. state = WinterSummerBot.STATE_ERROR
            # Attention, pas de sauvegarde.
            raise

    async def get_top_value(self, client: AsyncClient, symbol: str, history: str, interval_top:str):
        klines = await client.get_historical_klines(symbol, interval_top,history)
        top_value = max([Decimal(kline[2]) for kline in  # Top déduit de l'historique
                         klines])
        return top_value

    async def get_avg_last_interval(self, client, interval, symbol):
        klines = await client.get_klines(symbol=symbol, interval=interval)
        return sum(klines[-1][1:5]) / 4


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: AsyncClient,
              socket_manager: BinanceSocketManager,
              client_account: Dict[str, Any],
              bot_name: str,
              agent_queues: Dict[str, Queue],
              conf: Dict[str, Any]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = agent_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    json_generator = {}
    if path.exists():
        json_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={json_generator['state']}")
    # Puis initialisatio du generateur
    bot_generator = await WinterSummerBot.create(client,
                                                 bot_queue,
                                                 log,
                                                 json_generator,
                                                 socket_manager=socket_manager,
                                                 generator_name=bot_name,
                                                 client_account=client_account,
                                                 agent_queues=agent_queues,
                                                 conf=conf,
                                                 )
    while True:
        if await bot_generator.next() == STOPPED:
            return
        atomic_save_json(bot_generator, path)
