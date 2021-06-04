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
import sys
import traceback
from asyncio import QueueEmpty
from decimal import Decimal
from pathlib import Path

from aiohttp import ClientConnectorError
from binance import Client, BinanceSocketManager
from binance.enums import SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC, SIDE_SELL, ORDER_TYPE_MARKET
# Mémorise l'état de l'automate, pour permettre une reprise à froid
from binance.exceptions import BinanceAPIException
from binance.helpers import *

from conf import MIN_RECONNECT_WAIT
from filled_order import *
from multiplex_stream import add_multiplex_socket
from tools import atomic_load_json, generate_order_id, wait_queue_init, update_order, split_symbol, \
    atomic_save_json, check_order, log_order, update_wallet, json_order, log_add_order
from user_stream import add_user_socket

# TODO: a voir dans streams
#     MAX_RECONNECTS = 5
#     MAX_RECONNECT_SECONDS = 60
#     MIN_RECONNECT_WAIT = 0.1
#     TIMEOUT = 10
#     NO_MESSAGE_RECONNECT_TIMEOUT = 60

POOLING_SLEEP = 2

STATE_INIT = "init"
STATE_ALIGN_WINTER = "align_winter"
STATE_WAIT_ALIGN_WINTER_ORDER_FILLED = "wait_align_winter_filled"
STATE_ALIGN_SUMMER = "align_summer"
STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED = "wait_align_summer_filled"
STATE_WINTER_ORDER = "winter_order"
STATE_WAIT_WINTER_ORDER_FILLED = "wait_winter_order_filled"
STATE_WAIT_SUMMER = "wait_summer"
STATE_WAIT_SUMMER_ORDER_FILLED = "wait_summer_order_filled"

def dstr(d:Decimal):
    return format(d,"f")

async def to_usdt(client:AsyncClient,asset:str,val:Decimal) -> Decimal:
    if not val:
        return Decimal(0)
    if asset == "USDT":
        return val
    if asset == "ETH":
        return ((await client.get_symbol_ticker(symbol="ETHUSDT"))["price"] * val)
    if asset == "BTC":
        return ((await client.get_symbol_ticker(symbol="BTCUSDT"))["price"] * val)
    if asset in ["BIDR","BRL","BVND","DAI","IDRT","NGN","RUB","TRY","UAH"]: # FIXME: a tester. Inversion de la conv ?
        return (await client.get_symbol_ticker(symbol="USDT"+asset))["price"]
    return ((await client.get_symbol_ticker(symbol=asset+"USDT"))["price"] * val)

class Ctx(dict):
    def __init__(self, *args, **kwargs):
        super(Ctx, self).__init__(*args, **kwargs)
        self.__dict__ = self


async def bot(client: AsyncClient,
              socket_manager: BinanceSocketManager,
              client_account: Dict[str, Any],
              bot_name: str,
              agent_queues: Dict[str, Queue],  # All agent queues
              conf: Dict[str, Any]) -> None:
    # TODO: capture des exceptions globale pour alerte
    log = logging.getLogger(bot_name)
    wait_init_queue = True

    def save_state():
        # FIXME atomic_save_json(ctx, path)
        log.warning("State not saved")  # FIXME
        pass

    while True:
        try:
            # Conf par défaut
            ctx = Ctx(
                {
                    "state": STATE_INIT,
                    "order_state": None,
                    "wallet": {}
                })

            # Récupération du symbol
            symbol = conf["symbol"]
            lower_percent = Decimal(conf['winter'].strip('%')) / 100
            upper_percent = Decimal(conf['summer'].strip('%')) / 100

            if 'top' in conf:
                top_value = Decimal(conf["top"])
            else:
                top_value = max([Decimal(kline[2]) for kline in
                                 await client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MONTH,
                                                                    "5 years ago UTC")])
            ctx.target_summer = top_value * (1 + upper_percent)

            base, quote = split_symbol(symbol)
            balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
            balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))

            path = Path("ctx", bot_name + ".json")
            symbol_info = await client.get_symbol_info(symbol)

            input_queue = agent_queues[bot_name]  # Queue to receive msg for user or other agent
            market_queue = asyncio.Queue()  # Queue to receive event from market

            # Ajuster suivant le démarrage
            # Prendre la somme en USDT la plus grande pour choisir
            # ctx.state = STATE_WAIT_SUMMER # FIXME: a virer

            # Lecture de l'état courant de l'agent
            if path.exists():
                obj, rollback = atomic_load_json(path)
                ctx = Ctx(obj)
                if rollback:  # TODO: impossible de lire le tous dernier context de l'agent
                    ctx.previous_state = ctx.state
                    ctx.state = "resynchronize"  # FIXME
                ctx.wallet = {k:Decimal(v) for k,v in ctx.wallet.items() }
                # Ajuste le solde dispo dynamiquement
                # ctx.base_quantity = Decimal(ctx.base_quantity)
                # ctx.quote_quantity = Decimal(ctx.quote_quantity)
                # balance_base['agent_free'] -= ctx.base_quantity  # FIXME: c'est pas bon en cas de boucle
                # balance_quote['agent_free'] -= ctx.quote_quantity

            else:
                log.info(f"Started")

            # L'enregistrement des streams ne doit être fait qu'au début du traitement
            # Peux recevoir des messages non demandés
            # Dois rendre la main au plus vite. A vocation à modifier l'état pour laisser l'automate continuer
            async def event(msg: Dict[str, Any]) -> None:
                market_queue.put_nowait(msg)

            # add_multiplex_socket(symbol.lower()+"@ticker", event)
            add_multiplex_socket(symbol.lower() + "@aggTrade", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@trade", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@kline_1m", event)  # 1m
            # add_multiplex_socket(symbol.lower()+"@miniTicker", event)  # 1m

            # Ajout une queue pour attendre les évènements de l'utilisateur en websocket
            user_queue = asyncio.Queue()  # Queue to receive event from user account
            add_user_socket(lambda msg: user_queue.put_nowait(msg))

            # FIXME Clean all new order for symbol
            log.warning("Cancel all orders !")
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
            if 'winter_order_ctx' in ctx:
                winter_order = await restart_order_generator(client, user_queue, log, ctx.winter_order_ctx)
            if 'summer_order_ctx' in ctx:
                summer_order = await restart_order_generator(client, user_queue, log, ctx.summer_order_ctx)

            # Finite state machine
            while True:
                try:
                    # Reception d'ordre venant de l'API. Par exemple, ajout de fond, arret, etc.
                    msg = input_queue.get_nowait()
                    if msg['msg'] == 'kill':
                        log.warning("Receive kill")
                        return
                except QueueEmpty:
                    pass  # Ignore

                if ctx.state == STATE_INIT:
                    # Récupère le volume de token à partir des balances free
                    # puis convertie en USDT pour savoir lequel est le plus gros
                    # Cela permet de savoir la saison. L'autre devise est acheté ou vendu pour
                    # avoir l'un des soldes à zéro.
                    ctx.quote_quantity = Decimal(0)
                    ctx.base_quantity = Decimal(0)
                    quote_quantity=conf.get('quote_quantity',"0")
                    if '%' in quote_quantity:
                        ctx.quote_quantity = Decimal(balance_quote['agent_free'] * Decimal(
                            quote_quantity.strip('%')) / 100)
                    else:
                        ctx.quote_quantity = Decimal(quote_quantity)
                    ctx.wallet[quote] = ctx.quote_quantity

                    base_quantity=conf.get('base_quantity',"0")
                    if '%' in base_quantity:
                        ctx.base_quantity = Decimal(balance_base['agent_free'] * Decimal(base_quantity.strip('%')) / 100)
                    else:
                        ctx.base_quantity = Decimal(base_quantity)
                    ctx.wallet[base] = ctx.base_quantity
                    ctx.wallet[quote] = ctx.quote_quantity

                    base_usdt=await to_usdt(client,base,ctx.wallet[base])
                    quote_usdt=await to_usdt(client,quote,ctx.wallet[quote])

                    log.info(f"Start with {dstr(ctx.base_quantity)} {base} "
                             f"({dstr(base_usdt)}$) and "
                             f"{dstr(ctx.quote_quantity)} {quote} ({dstr(quote_usdt)}$)")
                    if base_usdt < quote_usdt:
                        # On est en ete
                        ctx.state = STATE_WINTER_ORDER if not base_usdt else STATE_ALIGN_WINTER
                        log.info("Start in winter")
                    else:
                        ctx.state = STATE_WAIT_SUMMER if not quote_usdt else STATE_ALIGN_SUMMER
                        log.info("Start in summer")
                    save_state()

                # Vend les 'base' pour n'avoir que du 'quote'
                elif ctx.state == STATE_ALIGN_WINTER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    order = {
                        "newClientOrderId": generate_order_id(bot_name),
                        "symbol": symbol,
                        "side": SIDE_SELL,
                        "type": ORDER_TYPE_MARKET,
                        "quoteOrderQty": ctx.wallet[base],
                    }
                    order = update_order(symbol_info, current_price, order)
                    check_order(symbol_info, current_price, order.get('price'), order.get('quantity'))
                    log.info(f"Try to {order['side']} {order['quoteOrderQty']} {base} at market ...")
                    await client.create_test_order(**json_order(order))
                    winter_order, ctx.winter_order_ctx = await create_order_generator(
                        client,
                        user_queue,
                        log,
                        order
                    )
                    ctx.state = STATE_WAIT_ALIGN_WINTER_ORDER_FILLED
                    save_state()
                elif ctx.state == STATE_WAIT_ALIGN_WINTER_ORDER_FILLED:
                    ctx.winter_order_ctx = await winter_order.asend(None)
                    if ctx.winter_order_ctx.state == STATE_ORDER_FILLED:
                        ctx.buy_quantity_base = Decimal(ctx.winter_order_ctx.order['executedQty'])
                        ctx.buy_price_base = Decimal(ctx.winter_order_ctx.order['price'])
                        log.info(f"{ctx.winter_order_ctx.order['side']} {ctx.buy_quantity_base} {base} at {ctx.buy_price_base}")
                        del ctx.winter_order_ctx
                        ctx.state = STATE_WINTER_ORDER
                        log.info(f"{ctx.wallet=}")
                        log.info("Wait the summer...")
                        save_state()
                    elif ctx.winter_order_ctx.state == STATE_ERROR:
                        # FIXME
                        ctx.state == STATE_ERROR
                        save_state()

                # Vend les 'quote' pour n'avoir que du 'base'
                elif ctx.state == STATE_ALIGN_SUMMER:
                    current_price = Decimal((await client.get_symbol_ticker(symbol=symbol))["price"])
                    order = {
                        "newClientOrderId": generate_order_id(bot_name),
                        "symbol": symbol,
                        "side": SIDE_BUY,
                        "type": ORDER_TYPE_MARKET,
                        "quoteOrderQty": ctx.wallet[quote],
                    }
                    order = update_order(symbol_info, current_price, order)
                    order['price']=Decimal("1528.93")
                    order['quantity'] = round_step_size(order['quantity'], symbol_info.lot.stepSize)
                    log_add_order(log,order)
                    await client.create_test_order(**json_order(order))
                    summer_order, ctx.summer_order_ctx = await create_order_generator(
                        client,
                        user_queue,
                        log,
                        order
                    )
                    ctx.state = STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED
                    save_state()
                elif ctx.state == STATE_WAIT_ALIGN_SUMMER_ORDER_FILLED:
                    ctx.summer_order_ctx = await summer_order.asend(None)
                    if ctx.summer_order_ctx.state == STATE_ORDER_FILLED:
                        ctx.wallet = update_wallet(ctx.wallet,ctx.summer_order_ctx.order)
                        ctx.buy_quantity_base = Decimal(ctx.summer_order_ctx.order['executedQty'])
                        # FIXME: le calcul avec fills
                        ctx.buy_price_base = Decimal(ctx.summer_order_ctx.order['fills'][0]['price'])
                        log_order(log, ctx.summer_order_ctx.order)
                        del ctx.summer_order_ctx
                        ctx.state = STATE_WINTER_ORDER
                        log.info(f"{ctx.wallet=}")
                        log.info(f"Wait the winters... ({dstr(ctx.wallet[base])} {base} / {dstr(ctx.wallet[quote])} {quote})")
                        save_state()
                    elif ctx.summer_order_ctx.state == STATE_ERROR:
                        # FIXME
                        ctx.state == STATE_ERROR
                        save_state()

                elif ctx.state == STATE_WINTER_ORDER:
                    interval = Client.KLINE_INTERVAL_1WEEK  # TODO: parametre ?
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    log.info(f"{current_price=}")
                    klines = await client.get_klines(symbol=symbol, interval=interval)
                    high = klines[-1][2]
                    low = klines[-1][3]
                    avg_price = (high + low) / 2
                    #log.info(f"{avg_price=}")
                    price = avg_price * (1 + lower_percent)
                    price = current_price # FIXME
                    quantity = ctx.wallet[quote] / price

                    order = {
                        "newClientOrderId": generate_order_id(bot_name),
                        "symbol": symbol,
                        "side": SIDE_BUY,
                        "type": ORDER_TYPE_LIMIT,
                        "timeInForce": TIME_IN_FORCE_GTC,
                        "quantity": quantity,
                        "price": price,
                    }
                    order = update_order(symbol_info, current_price, order, accept_upper=False)
                    log.info(f"{json_order(order)=}")
                    try:
                        await client.create_test_order(**json_order(order))
                        winter_order, ctx.winter_order_ctx = await create_order_generator(
                            client,
                            user_queue,
                            log,
                            order
                        )
                        log_add_order(log,order)
                        ctx.state = STATE_WAIT_WINTER_ORDER_FILLED
                        save_state()
                    except BinanceAPIException as ex:
                        if ex.code == -1013:  # OR -2011
                            # Trading impossible. (filtre non valide. Pas assez de fond ?) # TODO
                            log.error(ex.message)
                        else:
                            log.exception(ex)
                        ctx.state=STATE_ERROR
                        save_state()


                elif ctx.state == STATE_WAIT_WINTER_ORDER_FILLED:
                    ctx.winter_order_ctx = await winter_order.asend(None)
                    if ctx.winter_order_ctx.state == STATE_ORDER_FILLED:
                        ctx.wallet = update_wallet(ctx.wallet,ctx.winter_order_ctx.order)
                        ctx.buy_quantity_base = Decimal(ctx.winter_order_ctx.order['executedQty'])
                        ctx.buy_price_base = Decimal(ctx.winter_order_ctx.order['price'])
                        log.info(f"*** Buy {ctx.buy_quantity_base} {base} at {ctx.buy_price_base}")
                        del ctx.winter_order_ctx
                        ctx.state = STATE_WAIT_SUMMER
                        log.info(f"{ctx.wallet=}")
                        log.info("Wait the summer...")
                        save_state()
                    # TODO: si ordre expirer, le relancer
                    elif ctx.winter_order_ctx.state == STATE_ERROR:
                        # FIXME
                        ctx.state == STATE_ERROR
                        save_state()

                elif ctx.state == STATE_WAIT_SUMMER:
                    current_price = (await client.get_symbol_ticker(symbol=symbol))["price"]
                    log.info(f"current price ={current_price}")
                    #price = ctx.target_summer
                    price = current_price-100
                    quantity = ctx.wallet[base]  # On vent tout
                    order = {
                        "newClientOrderId": generate_order_id(bot_name),
                        "symbol": symbol,
                        "side": SIDE_SELL,
                        "type": ORDER_TYPE_LIMIT,
                        "quantity": quantity,
                        "timeInForce": TIME_IN_FORCE_GTC,
                        "price": price,
                    }
                    order = update_order(symbol_info, current_price, order)
                    # FIXME check_order(symbol_info, current_price, order['price'], order.get('quantity',None))
                    await client.create_test_order(**json_order(order))
                    summer_order, ctx.summer_order_ctx = await create_order_generator(
                        client,
                        user_queue,
                        log,
                        order
                    )
                    log_add_order(log,order)
                    ctx.state = STATE_WAIT_SUMMER_ORDER_FILLED
                    save_state()

                elif ctx.state == STATE_WAIT_SUMMER_ORDER_FILLED:
                    ctx.summer_order_ctx = await summer_order.asend(None)
                    if ctx.summer_order_ctx.state == STATE_ORDER_FILLED:
                        ctx.wallet = update_wallet(ctx.wallet,ctx.summer_order_ctx.order)
                        sell_quantity_quote = Decimal(ctx.summer_order_ctx.order['executedQty'])
                        sell_price_quote = Decimal(ctx.summer_order_ctx.order['price'])
                        log.info(f"*** Sell {sell_quantity_quote} {quote} at {sell_price_quote}")
                        del ctx.summer_order_ctx
                        # TODO: calcul du gain
                        ctx.state = STATE_WINTER_ORDER
                        log.info("Wait the winter...")
                        save_state()
                    elif ctx.summer_order_ctx.state == STATE_ERROR:
                        # FIXME
                        ctx.state == STATE_ERROR
                        save_state()
                    # TODO: wait la remote au dela du top
                elif ctx.state == STATE_ERROR:
                    log.error("State error")
                    await sleep(60 * 60)
                    # ou ajustement de l'ordre en cours
                    # evt = await market_queue.get()
                    # if evt['e'] == 'aggTrade':
                    #     # {
                    #     #   "e": "aggTrade",  // Event type
                    #     #   "E": 123456789,   // Event time
                    #     #   "s": "BNBBTC",    // Symbol
                    #     #   "a": 12345,       // Aggregate trade ID
                    #     #   "p": "0.001",     // Price
                    #     #   "q": "100",       // Quantity
                    #     #   "f": 100,         // First trade ID
                    #     #   "l": 105,         // Last trade ID
                    #     #   "T": 123456785,   // Trade time
                    #     #   "m": true,        // Is the buyer the market maker?
                    #     #   "M": true         // Ignore
                    #     # }
                    #     if evt['s'] == symbol:
                    #         current_price = Decimal(evt['p'])
                    #         if current_price >= target_summer:
                    #             log.info(f"I want to buy in summer at {current_price}")
                    # log.info(evt)
                    # if evt['e'] == 'error':
                    #     pass
                    # save_state()

        except (ClientConnectorError, BinanceAPIException, asyncio.TimeoutError) as ex:
            log.exception("Binance communication error")
            await sleep(MIN_RECONNECT_WAIT)
            log.info("Try to reconnect")
            # Restart the agent

        except Exception as ex:
            log.error(ex)
            traceback.print_tb(ex.__traceback__)
            sys.exit(-1)  # FIXME
