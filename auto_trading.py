import asyncio
import logging
import os
from decimal import *
from importlib import import_module
from typing import Union

import jstyleson as json

from TypingClient import TypingSymbolInfo, TypingClient

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = bool(os.environ.get("BINANCE_API_TEST", "False"))


def check_order(wsi: TypingSymbolInfo, symbol: str, current_price: Decimal, price: Union[Decimal, int],
                quantity: Decimal) -> bool:
    # price
    assert price >= wsi.price.minPrice
    assert price <= wsi.price.maxPrice
    assert (price - wsi.price.minPrice) % wsi.price.tickSize == 0

    # percent
    min_price = current_price * wsi.percent.multiplierDown
    max_price = current_price * wsi.percent.multiplierUp
    assert min_price <= price <= max_price

    # lot
    quantity >= wsi.lot.minQty
    quantity <= wsi.lot.maxQty
    assert (quantity - wsi.lot.minQty) % wsi.lot.stepSize == 0

    # TODO MIN_NOTIONAL
    assert wsi.min_notional.applyToMarket and price * quantity > wsi.min_notional.minNotional

    # TODO ICEBERG_PARTS (local, cas rare)

    # Market lot size
    assert quantity >= wsi.market_lot_size.minQty
    assert quantity <= wsi.market_lot_size.maxQty
    assert not wsi.market_lot_size.stepSize or (
            quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize == 0

    # TODO Max num order (global account)
    # TODO MAX_NUM_ALGO_ORDERS (global account)
    # MAX_NUM_ICEBERG_ORDERS (achat/vente de gros lot en petit morceau pour le cacher)
    # MAX_POSITION FILTER (global account)
    # EXCHANGE_MAX_NUM_ORDERS (global account)
    # EXCHANGE_MAX_NUM_ALGO_ORDERS (global account)
    return True


async def main():
    # initialise the client
    try:
        # {"verify": False, "timeout": 20}
        # client = await AsyncClient.create(api_key, api_secret, testnet=test_net)
        client = await TypingClient.create(api_key, api_secret, testnet=test_net)
        # client = Client(api_key, api_secret, testnet=test_net)

        with open("conf.json") as f:
            conf = json.load(f)

        # Création des agents à partir de conf.json.
        # Le nom de l'agent correspond au module à importer. Le code dans etre dans une async fonction agent(...).
        # Ou bien,
        # Dans les paramètres de l'agent, il peut y avoir un paramètre 'function' pour indiquer la fonction à appliquer
        # sinon, c'est le nom de l'agent qui est utilisé. Cela permet d'invoquer le même agent avec plusieurs jeux de
        # parametres.
        # Le nom peut se limiter au module, ou etre complet (module or module.ma_fonction)
        agents = []
        for agent in conf:
            agent_name = list(agent.keys())[0]
            conf = agent[agent_name]
            fn_name = conf["function"] if "function" in conf else agent_name
            if '.' not in fn_name:
                fn_name += ".agent"
            module_path, fn = fn_name.rsplit(".", 1)
            async_fun = getattr(import_module(module_path), fn)

            agents.append(loop.create_task(async_fun(client, agent_name, conf)))  # Start agent
        await asyncio.gather(*agents, return_exceptions=True)  # Lance tous les agents en //
    finally:
        await client.close_connection()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
