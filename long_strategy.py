"""
Cette stratégie cherche a exploiter les grands écarts sur les cryptos, en partant du fait qu'il y a de temps
en temps des fortes chutes de X%, puis que le cours fini par dépasser le top.

L'idée est de capturer les chutes fortes, pour entrer sur le marché. Puis de revendre au dépassement du top.
Parametres:
{
      "symbol": "BNBBUSD",  # La paire à trader
      "base_quantity": "0.1%", # La quantité de 'base' à utiliser (ou zéro)
      "quote_quantity": "0.1%", # La quantité de 'quote' à utiliser (ou zéro mais l'un des deux)
      "period": "1 week" # Moyenne entre top et bottom de la période précédente comme point de d'analyse
      "winter": "-30%", # Si le marché tombe de 'winter' % sur la période, alors achète
      "summer": "2%" # Si le marché remonte au dessus du top, de 'summer' %, alors vend
}
"""
import asyncio
import logging
import sys
import traceback
from asyncio import QueueEmpty
from decimal import Decimal
from pathlib import Path

from aiohttp import ClientConnectorError
from binance import AsyncClient, Client
from binance.enums import SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC, ORDER_TYPE_MARKET

# Mémorise l'état de l'automate, pour permettre une reprise à froid
from binance.exceptions import BinanceAPIException

from conf import MIN_RECONNECT_WAIT
from filled_order import *
from multiplex_stream import add_multiplex_socket
from tools import atomic_load_json, generate_order_id, wait_queue_init, check_order, update_order, split_symbol, \
    atomic_save_json
from user_stream import add_user_socket

# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60

POOLING_SLEEP = 2

STATE_INIT = "init"
STATE_WINTER_ORDER = "winter_order"
STATE_WAIT_WINTER_ORDER_FILLED = "wait_winter_order_filled"
STATE_WAIT_SUMMER = "wait_summer"
STATE_WAIT_SUMMER_ORDER_FILLED = "wait_summer_order_filled"

class Ctx(dict):
    def __init__(self, *args, **kwargs):
        super(Ctx, self).__init__(*args, **kwargs)
        self.__dict__ = self


async def bot(client: AsyncClient,
              client_account:Dict[str,Any],
              bot_name: str,
              agent_queues: Dict[str, Queue],  # All agent queues
              conf: Dict[str, Any]) -> None:
    # TODO: capture des exceptions globale pour alerte
    log= logging.getLogger(bot_name)
    wait_init_queue = True

    def save_state():
        # atomic_save_json(ctx, path)
        pass

    while True:
        try:
            # Récupération du symbol
            interval=Client.KLINE_INTERVAL_1WEEK
            symbol = conf["symbol"]
            lower_percent = Decimal(conf['winter'].strip('%'))/100
            upper_percent = Decimal(conf['summer'].strip('%'))/100
            base,quote = split_symbol(symbol)
            balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
            balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))

            path = Path("ctx", bot_name + ".json")
            symbol_info = await client.get_symbol_info(symbol)

            input_queue = agent_queues[bot_name]  # Queue to receive msg for user or other agent
            market_queue = asyncio.Queue()  # Queue to receive event from market


            # Conf par défaut
            ctx = Ctx(
                {
                    "state": STATE_INIT,
                    "order_state": None
                })

            ctx.state = STATE_WAIT_SUMMER # FIXME: a virer
            # Lecture de l'état courant de l'agent
            if path.exists():
                obj, rollback = atomic_load_json(path)
                ctx = Ctx(obj)
                if rollback:  # TODO: impossible de lire le tous dernier context de l'agent
                    ctx.previous_state = ctx.state
                    ctx.state = "resynchronize"  # FIXME
                # Ajuste le solde dispo dynamiquement
                ctx.base_quantity=Decimal(ctx.base_quantity)
                ctx.quote_quantity=Decimal(ctx.quote_quantity)
                balance_base['agent_free'] -= ctx.base_quantity  # FIXME: c'est pas bon en cas de boucle
                balance_quote['agent_free'] -= ctx.quote_quantity

            else:
                log.info(f"Started")

            # L'enregistrement des streams ne doit être fait qu'au début du traitement
            # Peux recevoir des messages non demandés
            # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
            async def event(msg: Dict[str, Any]) -> None:
                market_queue.put_nowait(msg)
            add_multiplex_socket(symbol.lower()+"@ticker", event)

            # Ajout une queue pour attendre les évènements de l'utilisateur en websocket
            user_queue = asyncio.Queue()  # Queue to receive event from user account
            add_user_socket(lambda msg: user_queue.put_nowait(msg))

            # FIXME Clean all new order for symbol
            for order in await client.get_all_orders(symbol=symbol):
                if order["status"] == ORDER_STATUS_NEW:
                    await client.cancel_order(symbol=order["symbol"], orderId=order["orderId"])

            # List open order
            # open_orders=await client.get_open_orders(symbol='BNBBTC')
            # for order in open_orders:
            #     log.info(f'Open order {order["orderId"]}')

            # Attend le démarrage des queues user et multiplex
            if wait_init_queue:
                await wait_queue_init(input_queue)
                wait_init_queue = False

            # info = await client.get_account()
            # for b in info['balances']:
            #     print(f'{b["asset"]}:{b["free"]}/{b["locked"]}')

            # Reprise du generateur order
            if 'order_ctx' in ctx:
                current_order = await restart_order_generator(client, user_queue, log, ctx.order_ctx)

            # Finite state machine
            while True:
                try:
                    # Reception d'ordre venant de l'API. Par exemple, ajout de fond, arret, etc.
                    msg = input_queue.get_nowait()
                except QueueEmpty:
                    pass  # Ignore

                if ctx.state == STATE_INIT:
                    # La balance est calculé au démarrage de l'agent
                    if '%' in conf['quote_quantity']:
                        quantity_percent = Decimal(conf['quote_quantity'].strip('%')) / 100
                        ctx.base_quantity = Decimal(0)
                        ctx.quote_quantity = balance_quote['agent_free'] * quantity_percent
                        # TODO: ajustement des quantités
                    else:
                        ctx.base_quantity = Decimal(0)
                        ctx.quote_quantity = conf['quote_quantity']
                    ctx.quote_quantity = ctx.quote_quantity.normalize()
                    balance_quote['agent_free'] -= ctx.quote_quantity

                    log.info(f"Start with {ctx.base_quantity} {base} and {ctx.quote_quantity} {quote}")
                    ctx.state = STATE_WINTER_ORDER
                    # [
                    #     [
                    #         1499040000000,      # 0: Open time
                    #         "0.01634790",       # 1: Open
                    #         "0.80000000",       # 2: High
                    #         "0.01575800",       # 3: Low
                    #         "0.01577100",       # 4: Close
                    #         "148976.11427815",  # 5: Volume
                    #         1499644799999,      # 6: Close time
                    #         "2434.19055334",    # 7: Quote asset volume
                    #         308,                # 8: Number of trades
                    #         "1756.87402397",    # 9: Taker buy base asset volume
                    #         "28.46694368",      # 10: Taker buy quote asset volume
                    #         "17928899.62484339" # 11: Can be ignored
                    #     ]
                    # ]
                    save_state()

                elif ctx.state == STATE_WINTER_ORDER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    klines = await client.get_klines(symbol=symbol, interval=interval)
                    high = klines[-1][2]
                    low = klines[-1][3]
                    avg_price = (high + low) / 2

                    #price = avg_price * (1 + lower_percent)
                    price = current_price # FIXME
                    quantity = ctx.quote_quantity

                    order = {
                        "newClientOrderId": generate_order_id(bot_name),
                        "symbol": symbol,
                        "side": SIDE_BUY,
                        # A utiliser pour entrer dans le marché ?
                        # "type": ORDER_TYPE_MARKET,
                        # "quoteOrderQty": ctx.balance_quote,  # Achat autant que possible pour cette somme au market
                        "type": ORDER_TYPE_LIMIT,
                        "quantity": quantity,
                        "timeInForce": TIME_IN_FORCE_GTC,
                        "price": price,
                    }
                    order = update_order(symbol_info, current_price, order)
                    # FIXME check_order(symbol_info, current_price, order['price'], order.get('quantity',None))
                    await client.create_test_order(**order)

                    # FIXME current_order = order_generator(client, agent_name+"-order", ctx.order_state)
                    current_order, ctx.order_ctx = await create_order_generator(
                        client,
                        user_queue,
                        log,
                        order
                    )
                    ctx.state = STATE_WAIT_WINTER_ORDER_FILLED
                    save_state()

                elif ctx.state == STATE_WAIT_WINTER_ORDER_FILLED:
                    ctx.order_ctx = await current_order.asend(None)
                    if ctx.order_ctx.state == STATE_ORDER_FILLED:
                        del ctx.order_ctx
                        ctx.state = STATE_WAIT_SUMMER
                        save_state()
                    elif ctx.order_ctx.state == STATE_ERROR:
                        # FIXME
                        ctx.state == STATE_ERROR
                        save_state()

                elif ctx.state == STATE_WAIT_SUMMER:
                    # TODO: wait la remote au dela du top
                    # ou ajustement de l'ordre en cours
                    pass
                    evt = await market_queue.get()
                    log.info(evt)
                    if evt['e'] == 'error':
                        pass
                    # save_state()

                elif ctx.state == STATE_WAIT_SUMMER_ORDER_FILLED:
                    # TODO: après  la vente haut, attend la prochaine forte baisse
                    ctx.state = STATE_WINTER_ORDER
                    save_state()
                    pass
        except (ClientConnectorError,BinanceAPIException,asyncio.TimeoutError) as ex:
            log.exception("Binance communication error")
            await sleep(MIN_RECONNECT_WAIT)
            log.info("Try to reconnect")
            # Restart the agent

        except Exception as ex:
            log.error(ex)
            traceback.print_tb(ex.__traceback__)
            sys.exit(-1)  # FIXME
