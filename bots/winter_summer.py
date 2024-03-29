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
      "interval_top": "1w" (optional) # Interval pour trouver le top précédant
      "winter": "-30%", # Si le marché tombe de 'winter' % sur la période, alors achète
      "summer": "2%" # Si le marché remonte au dessus du top, de 'summer' %, alors vend
}

Au début, aligne les 2 coins pour n'en avoir qu'un seul, suivant le plus gros.
Ce n'est pas toujours possible, à cause des limites de ventes et d'achats de la paire.
Pour le moment, cela génère une erreur, avant de trouver mieux.
"""
"""
TODO

Trailing buy:
- Au prix limit, place un min au prix limit, et un top a limit +5%
- le min continue à descendre
- le top 5% suit le min
- lorsque le prix dépasse le min +5%, achete

Take profit trailing https://help.3commas.io/en/articles/3108982-trailing-take-profit-catch-the-rise
- On calcul le take profit en % (genre 10%)
- on déduit la déviation (genre 2%) pour déclancher à 10%-2%
- la on apprend (suivit du top, si baisse de déviation sous le top, vend)
"""
# TODO: client.get_lending_product_list() pour stacker en attendant, en été
from asyncio import QueueEmpty

import aiohttp
from aiohttp import ClientConnectorError
from binance.enums import *
from stream_multiplex import add_multiplex_socket

from add_order import *
from bot_generator import STOPPED
from simulate_client import *
from tools import atomic_load_json, generate_order_id, wait_queue_init, update_order, atomic_save_json, check_order, \
    json_order, log_add_order, to_usdt, str_order

# Mémorise l'état de l'automate, pour permettre une reprise à froid


# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60

MINIMUM_REDUCE_PRICE = Decimal("0.99")


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class WinterSummerBot(BotGenerator):
    # self.last_price c'est le prix de la dernière transaction

    POOLING_SLEEP = 2 * sleep_speed()

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
            interval_top = conf.get("interval_top", KLINE_INTERVAL_1WEEK)
            history_top = conf.get("history_top", "5 years before UTC")
            if 'top' in conf:
                self.top_value = Decimal(conf["top"])  # Top fixé
            else:
                self.top_value = await self.get_top_value(client, symbol, history_top, interval_top)

            log.info(f"Start with top={self.top_value}")

            # ---- Gestion des queues de communications asynchones

            # L'enregistrement des streams ne doit être fait qu'au début du traitement
            # Peux recevoir des messages non demandés
            # Dois rendre la bots_engine au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
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
            # await engine.send_telegram(log,"Cancel all orders !")
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
                    msg = bot_queue.get_nowait()
                    if msg['e'] == 'kill':
                        await engine.send_telegram(log,"Receive kill")
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

                    log.info(f"Start with {self.base_quantity} {base} "
                             f"(${base_usdt}) and "
                             f"{self.quote_quantity} {quote} (${quote_usdt})")
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
                    self.top_value = max(current_price,self.top_value)

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
                        await client.create_test_order(**json_order(str_order(order)))
                        log_add_order(log, order, prefix="For align, try to ")
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
                        self.last_price=self.winter_order.order['price']
                        del self.winter_order
                        self.state = WinterSummerBot.STATE_ALIGN_WINTER
                        log_wallet(log, self.wallet)
                        log.info("Loop...")
                        yield self
                    elif self.winter_order.is_error():
                        # FIXME
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                # Vend les 'quote' pour n'avoir que du 'base'
                elif self.state == WinterSummerBot.STATE_ALIGN_SUMMER:
                    current_price = Decimal((await client.get_symbol_ticker(symbol=symbol))["price"])
                    self.top_value = max(current_price,self.top_value)

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
                    await client.create_test_order(**json_order(str_order(order)))
                    log_add_order(log, order, prefix="For align, try to ")
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
                        self.last_price=self.summer_order.order['price']
                        await engine.log_order(log, self.summer_order.order)
                        del self.summer_order
                        self.state = WinterSummerBot.STATE_WINTER_ORDER
                        log_wallet(log, self.wallet)
                        log.info(
                            f"Wait the winters... ({self.wallet[base]:f} {base} / {self.wallet[quote]:f} {quote})")
                        yield self
                    elif self.summer_order.is_error():
                        # FIXME
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                elif self.state == WinterSummerBot.STATE_WINTER_ORDER:
                    # Calcul la moyenne de la période précédente, et ajuste un ordre d'achat
                    # sur cette base.
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    self.top_value = max(current_price,self.top_value)

                    # log.info(f"{current_price=}")
                    self.avg_price = await self.get_avg_last_interval(client, interval, symbol)
                    # log.info(f"Prix moyen de la période précédente {self.avg_price} {quote}")

                    # Le prix cible d'achat, c'est une base de la moyenne précédente
                    price = Decimal(self.avg_price) * (1 + lower_percent)

                    log.info(f"Je cherche à acheter lorsque le prix est < à {self.avg_price} {lower_percent * 100}% = {price} {quote}")
                    if current_price < price:
                        # adjusted_price= current_price * (1 + lower_percent)
                        adjusted_price= current_price
                        await engine.send_telegram(log,
                            # f"J'ai vendu trop bas à {self.last_price} {quote}.\n"
                            # f"Le prix actuel est de {current_price} {quote}\n"
                            f"Le prix cible d'achat par rapport à la moyenne précédente est de {price} {quote} "
                            f"mais le prix actuel est déjà plus bas, à {current_price} {quote}. "
                            f"J'ajuste la cible d'achat à {adjusted_price} {quote} "
                            f"basé sur le prix courant."
                        )
                        price = adjusted_price

                    if price > current_price:
                        # FIXME ajuster à la baisse, ou en profiter pour acheter en traquant la baisse ?.
                        await engine.send_telegram(log,
                            f"Le prix calculé cible d'achat à la baisse, par rapport à la période précédant "
                            f"s'avere > au dernier prix actuel (avg:{self.avg_price} cible:{price} > cur:{current_price}). "
                            f"Le cours est descendu encore plus bas. "
                            f"J'ajuste la cible d'achat par rapport au prix actuel à {current_price * (1 + lower_percent)}")
                        price = current_price * (1 + lower_percent)

                    # if (price > self.last_price):
                    #     await engine.send_telegram(log,f"****** Je risque de perde, en essayant d'acheter plus chère ({price} {quote}) "
                    #                 f"que ma dernière vente ({self.last_price} {quote})")
                    quantity = self.wallet[quote] / price

                    # Et je lance un ordre d'achat bas
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

                    # Je calcul quand rafraichir la cible basse, par rapport à la période précédante
                    delta = interval_to_milliseconds(interval) / 4
                    self.next_refresh = get_now() + delta
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
                        if get_now() > self.next_refresh:
                            avg_price = await self.get_avg_last_interval(client, interval, symbol)
                            if avg_price != self.avg_price:
                                # log.info(f"Prix moyen de la période précédente {avg_price} {quote}")
                                self.avg_price = avg_price
                                self.winter_order.cancel()
                                self.state = WinterSummerBot.STATE_WAIT_WINTER_CANCEL_ORDER
                                yield self
                            # else:
                            #     log.debug("same price, continue with the current trade")
                            continue

                    if self.winter_order.is_filled():
                        if (self.winter_order.order['price'] > self.last_price):
                            await engine.send_telegram(log,
                                f"****** J'ai perdu en acheter plus chère ({price} {quote}) "
                                f"que ma dernière vente ({self.last_price} {quote})")
                        self.last_price=self.winter_order.order['price']
                        benefice(log, symbol, self.wallet, self.base_quantity, self.quote_quantity)
                        del self.winter_order
                        self.state = WinterSummerBot.STATE_WAIT_SUMMER
                        log_wallet(log, self.wallet)
                        self.top_value = await self.get_top_value(client, symbol, history_top, interval_top)
                        self.target_summer = self.top_value * (1 + upper_percent)
                        log.info(f"Nouvelle cible de vente = {self.top_value} + {upper_percent*100}% = {self.target_summer}")
                        log.info("Wait the summer...")
                        yield self
                    # TODO: si ordre expirer, le relancer
                    elif self.winter_order.is_error():
                        # FIXME: L'ordre est en erreur. Quoi faire ?
                        self.state == WinterSummerBot.STATE_ERROR
                        yield self

                elif self.state == WinterSummerBot.STATE_WAIT_SUMMER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    self.top_value = max(current_price,self.top_value)

                    price = self.target_summer # Cherche à vendre à la cible calculé lors du changement de saison
                    if self.target_summer < self.last_price:
                        await engine.send_telegram(log,f"L'objectif de vente au top ({self.target_summer} {quote}), "
                                    f"basé sur le top connu lors du passage a SUMMER, "
                                    f"est sous mon dernier prix d'achat {self.last_price} {quote}. "
                                    f"J'ajuste à {upper_percent * 100}% du nouveau plus haut. "
                                    f"Change to {max(self.last_price,current_price) * (1 + upper_percent)}")
                        price = max(self.last_price, current_price) * (1 + upper_percent)
                    # TODO: track sell
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
                        self.last_price=self.summer_order.order['price']
                        benefice(log, symbol, self.wallet, self.base_quantity, self.quote_quantity)
                        log_wallet(log, self.wallet)
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
                log.error(self.order)
                log.exception(ex)
                self.state = WinterSummerBot.STATE_ERROR
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request is outside of the recvWindow.':
                log.error(ex.message)
                # self.state = WinterSummerBot.STATE_ERROR
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request was 1000ms ahead of the server\'s time.':
                log.error(ex.message)
                # self.state = WinterSummerBot.STATE_ERROR
                yield self
            else:
                log.exception("Unknown error")
                log.exception(ex)
                self.state = WinterSummerBot.STATE_ERROR
                yield self


        except (ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
            self.state = WinterSummerBot.STATE_ERROR
            # Attention, pas de sauvegarde.
            raise

    async def get_top_value(self, client: AsyncClient, symbol: str, history: str, interval_top: str):
        klines = await client.get_historical_klines(symbol, interval_top, history)
        top_value = max([Decimal(kline[2]) for kline in  # Top déduit de l'historique
                         klines])
        return top_value

    async def get_avg_last_interval(self, client, interval, symbol):
        klines = await client.get_klines(symbol=symbol, interval=interval)
        assert klines
        return sum(klines[-1][1:5]) / 4


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: AsyncClient,
              client_account: Dict[str, Any],
              bot_name: str,
              engine: 'Engine',
              conf: Dict[str, str]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    socket_manager = client.getBinanceSocketManager()
    bot_queue = engine.agent_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    json_generator = {}
    if not global_flags.simulate and path.exists():
        json_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={json_generator['state']}")
    # Puis initialisatio du generateur
    bot_generator = await WinterSummerBot.create(client,
                                                 bot_queue,
                                                 log,
                                                 json_generator,
                                                 generator_name=bot_name,
                                                 client_account=client_account,
                                                 conf=conf,
                                                 )
    try:
        while True:
            if await bot_generator.next() == STOPPED:
                break
            if not global_flags.simulate:
                atomic_save_json(bot_generator, path)
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log,bot_generator.wallet)
        raise
