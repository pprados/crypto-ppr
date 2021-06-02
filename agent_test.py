from typing import Dict, Any

from binance import AsyncClient, BinanceSocketManager
from binance.helpers import round_step_size


async def bot(client: AsyncClient,
              name: str,
              conf: Dict[str, Any]) -> None:
    symbol = 'BNBBTC'
    amount = 0.000234234
    precision = 5
    amt_str = f"{amount:.{precision}f}"
    round_step_size(amount, precision)  # -> CDecimal

    # print(json_dumps(await client.get_account()))
    # print("My trades\n"+json_dumps(await client.get_my_trades(symbol='BNBBTC')))

    # client = await AsyncClient.create(api_key, api_secret, testnet=test_net)
    symbol_info = await client.get_symbol_info(symbol)
    # print(json_dumps(symbol_info))
    current = await client.get_symbol_ticker(symbol=symbol)
    # print(f"{current=}")

    # fetch exchange info
    # print(f"Exchange info\n{json_dumps(await client.get_exchange_info())}")

    # print(f"System status\n{json_dumps(await client.get_system_status())}")
    # print(f"Symbol info\n{await client.get_symbol_info(symbol)}")
    # print(f"Account snapshot\n{json_dumps(await client.get_account_snapshot(type='SPOT'))}")

    # print("Klines\n"+await client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_30MINUTE))
    #
    # # fetch 1 minute klines for the last day up until now
    # klines = await client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1MINUTE, "1 day ago UTC")

    # # fetch 30 minute klines for the last month of 2017
    # klines = await client.get_historical_klines(symbol, Client.KLINE_INTERVAL_30MINUTE, "1 Dec, 2017",
    #                                             "1 Jan, 2018")

    # fetch weekly klines since it listed
    # print(f'{await client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1WEEK, "1 Jan, 2017")=}')

    # print(f'avg price={await client.get_avg_price(symbol=symbol)=}')
    # C'est la profondeur
    # print(f"profondeur:\n {await client.get_order_book(symbol='BNBBTC')}")

    # avg_price = (await client.get_avg_price(symbol=symbol))['price']

    # le journal d'ordre
    # print(f"{await client.get_recent_trades(symbol=symbol)}")

    # print(f"{await client.get_aggregate_trades(symbol='BNBBTC')=}")

    quantity = 10
    price = current
    # check_order(symbol_info, symbol, current, price, quantity)

    # Essaye un ordre. Il recoit une erreur de binance si besoin
    # order = await client.create_test_order(
    #     symbol=symbol,
    #     side=SIDE_BUY,
    #     type=ORDER_TYPE_LIMIT,
    #     timeInForce=TIME_IN_FORCE_GTC,
    #     quantity=quantity,
    #     price=price
    # )
    #
    # order = await client.create_order(
    #     symbol=symbol,
    #     side=SIDE_BUY,
    #     type=ORDER_TYPE_LIMIT,
    #     timeInForce=TIME_IN_FORCE_GTC,
    #     quantity=quantity,
    #     price=price)
    # print(order)

    # -----------------------
    bm = BinanceSocketManager(client._delegate, user_timeout=60)
    # start any sockets here, i.e a trade socket
    ts = bm.trade_socket('BTCUSDT')
    # ts = bm.multiplex_socket(['bnbbtc@aggTrade', 'neobtc@ticker'])
    # ts = bm.symbol_ticker_socket(symbol="btcusdt")
    # ts = bm.kline_socket(symbol=symbol)
    # then start receiving messages
    asyncio.gather(event_loop(ts))
    # task = asyncio.create_task(event_loop(ts))
    # await task
    print("entre dans le sleep")
    await sleep(20)
    print("sleep terminé")
    await client.close_connection()
    print("Terminé normal")
    sys.exit(-1)
