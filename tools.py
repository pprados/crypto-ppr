import logging
import os
import re
from asyncio import Queue
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from random import randint
from typing import Any, Tuple, Dict

import jstyleson as json
from binance.enums import ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET, SIDE_BUY


def _parse_order(order: Dict[str, Any]) -> Tuple[str, str, Decimal, Decimal]:
    base, quote = split_symbol(order['symbol'])
    side = order['side']
    quantity = None
    price = None
    if 'price' in order:
        price = Decimal(order['price'])
    if 'fills' in order:
        # TODO [{'price': '2543.14000000', 'qty': '0.03932000', 'commission': '0.00000000', 'commissionAsset': 'ETH', 'tradeId': 1135}]
        fills = order['fills']
        if fills:
            price = Decimal(fills[0]['price'])  # FIXME
    if 'executedQty' in order:
        quantity = Decimal(order['executedQty'])
    if 'quantity' in order:
        quantity = Decimal(order['quantity'])
    if side == SIDE_BUY:
        token = base
    else:
        token = quote
    return side, token, quantity, price


def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        s = format(obj, "f")
        if '.' not in s:
            s = s + ".0"
        return s.rstrip('0')

    return obj.__dict__


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=_serialize)


def json_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json_dumps(order))


def atomic_save_json(obj: Any, filename: Path) -> None:
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    with open(new_filename, "w") as f:
        json.dump(obj, f, default=_serialize, indent=2)
    os.sync()
    if filename.exists():
        filename.rename(old_filename)
    new_filename.rename(filename)
    old_filename.unlink(missing_ok=True)
    os.sync()


def atomic_load_json(filename: Path) -> Tuple[Any, bool]:
    rollback = False
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    if new_filename.exists():
        # Try to load the new filename
        try:
            with open(new_filename) as f:
                obj = json.load(f)  # Try to parse
            filename.unlink(missing_ok=True)
            old_filename.unlink(missing_ok=True)
            new_filename.rename(filename)
            os.sync()
            return obj, False
        except JSONDecodeError as e:
            # new filename is dirty
            new_filename.unlink()
            filename.unlink(missing_ok=True)
            old_filename.rename(filename)
            os.sync()
            rollback = True
    if old_filename.exists():
        # Cela a crashé lors d'un JSONDecodeError, pendant qu'on resoud l'état.
        filename.unlink(missing_ok=True)
        old_filename.rename(filename)
        os.sync()
        rollback = True

    with open(filename) as f:
        return json.load(f), rollback


def generate_order_id(agent_name: str):
    return agent_name + "-" + str(randint(100000, 999999))


async def wait_queue_init(input_queue: Queue) -> None:
    """ Attent l'initiatilisation des queues user et multiplexe """
    user_queue_initilized = False
    multiplex_queue_initilized = False
    while not user_queue_initilized or not multiplex_queue_initilized:
        msg = await input_queue.get()
        if msg["from"] == "user_stream" and msg["msg"] == "initialized":
            user_queue_initilized = True
        if msg["from"] == "multiplex_stream" and msg["msg"] == "initialized":
            multiplex_queue_initilized = True


def update_order(wsi: Dict[str, Any], current_price: Decimal, order: Dict[str, Any], accept_upper=True) -> Dict[
    str, Any]:
    """ Ajute l'ordre pour être conforme aux contraintes de Binance.
     :param accept_upper True if accept to pay little more price. Else raise an exception.
     """
    side, token, quantity, price = _parse_order(order)
    if price and order["type"] == ORDER_TYPE_LIMIT:
        # Ajustement éventuelle du prix
        if price < wsi.price.minPrice:
            price = wsi.price.minPrice
        if price > wsi.price.maxPrice:
            price = wsi.price.maxPrice
        if (price - wsi.price.minPrice) % wsi.price.tickSize != 0:
            price = price - (price - wsi.price.minPrice) % wsi.price.tickSize
        assert (price - wsi.price.minPrice) % wsi.price.tickSize == 0

        # percent
        min_price = current_price * wsi.percent.multiplierDown
        max_price = current_price * wsi.percent.multiplierUp
        if price < min_price:
            price = min_price
        if price > max_price:
            price = max_price

    if quantity:
        # lot
        if quantity < wsi.lot.minQty:
            quantity = wsi.lot.minQty
        if quantity > wsi.lot.maxQty:
            quantity = wsi.lot.maxQty

        if (quantity - wsi.lot.minQty) % wsi.lot.stepSize != 0:
            quantity = quantity - ((Decimal(quantity) - wsi.lot.minQty) % wsi.lot.stepSize)

        # Market lot size
        if quantity < wsi.market_lot_size.minQty:
            quantity = wsi.market_lot_size.minQty
        if quantity > wsi.market_lot_size.maxQty:
            quantity = wsi.market_lot_size.maxQty
        if wsi.market_lot_size.stepSize:
            if (quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize != 0:
                quantity = quantity - ((quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize)

        # Market lot size
        if quantity < wsi.market_lot_size.minQty:
            quantity = wsi.market_lot_size.minQty
        if quantity > wsi.market_lot_size.maxQty:
            quantity = wsi.market_lot_size.maxQty

        if wsi.market_lot_size.stepSize:
            if (quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize != 0:
                quantity = quantity - ((quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize)

        # MIN_NOTIONAL (https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#min_notional)
        if wsi.min_notional.applyToMarket and order["type"] == ORDER_TYPE_MARKET:
            while current_price * quantity < wsi.min_notional.minNotional:
                quantity += wsi.lot.minQty
        elif order["type"] == ORDER_TYPE_LIMIT:
            # while price * quantity < wsi.min_notional.minNotional:
            #     quantity += wsi.lot.minQty
            pass  # FIXME
        if not accept_upper:
            if order["type"] == ORDER_TYPE_LIMIT:
                if order['quantity'] * order['price'] < quantity * price:
                    raise ValueError("Impossible to update the price or quantity")
            elif quantity > order['quantity']:
                raise ValueError("Impossible to update the quantity")
        if 'quantity' not in order:
            assert ("BUG")
        order['quantity'] = float(quantity)  # round_step_size(quantity, wsi.lot.stepSize)
    if order["type"] == ORDER_TYPE_LIMIT:
        order["price"] = float(price)

    # FIXME check_order(wsi, current_price, order)

    return order


def check_order(wsi: Dict[str, Any], current_price: Decimal, order) -> bool:
    side, token, quantity, price = _parse_order(order)
    # price
    if price:
        assert price >= wsi.price.minPrice
        assert price <= wsi.price.maxPrice
        assert (price - wsi.price.minPrice) % wsi.price.tickSize == 0

        # percent
        min_price = current_price * wsi.percent.multiplierDown
        max_price = current_price * wsi.percent.multiplierUp
        assert min_price <= price <= max_price

    if quantity:
        # lot
        assert quantity >= wsi.lot.minQty
        assert quantity <= wsi.lot.maxQty
        assert quantity % wsi.lot.stepSize == 0

        # TODO ICEBERG_PARTS (local, cas rare)

        # Market lot size
        assert quantity >= wsi.market_lot_size.minQty
        assert quantity <= wsi.market_lot_size.maxQty
        assert not wsi.market_lot_size.stepSize or \
               (quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize == 0

        # MIN_NOTIONAL
        assert price * quantity > wsi.min_notional.minNotional

    # TODO Max num order (global account)
    # TODO MAX_NUM_ALGO_ORDERS (global account)
    # MAX_NUM_ICEBERG_ORDERS (achat/vente de gros lot en petit morceau pour le cacher)
    # MAX_POSITION FILTER (global account)
    # EXCHANGE_MAX_NUM_ORDERS (global account)
    # EXCHANGE_MAX_NUM_ALGO_ORDERS (global account)
    return True


def split_symbol(symbol: str) -> Tuple[str, str]:
    m = re.match(r'(\w+)((USDT)|(ETH)|(BTC)|(USDT))$', symbol)
    return m.group(1), m.group(2)


def _dump_order(log: logging, order: Dict[str, Any], prefix: str, suffix: str = ''):
    side, token, quantity, price = _parse_order(order)
    str_price = "market" if order['type'] == ORDER_TYPE_MARKET else str(float(price))
    log.info(f"{prefix}{side} {float(quantity)} {token} at {str_price}{suffix}")


def log_add_order(log: logging, order: Dict[str, Any]):
    _dump_order(log, order, "Try ", "...")


def log_order(log: logging, order: Dict[str, Any]):
    _dump_order(log, order, "*** ")


def update_wallet(wallet: Dict[str, Decimal], order: Dict[str, Any]) -> Dict[str, Decimal]:
    """ Mise à jour du wallet"""
    base, quote = split_symbol(order['symbol'])
    side, token, quantity, price = _parse_order(order)
    old_wallet = wallet.copy()
    if side == SIDE_BUY:
        wallet[base] += quantity
        wallet[quote] -= quantity * price  # FIXME: peut être négatif :-(
    else:
        wallet[base] -= quantity
        wallet[quote] += quantity * price
    if wallet[base] < 0:
        print("error")
    if wallet[quote] < 0:
        print("error")
    # assert wallet[base] >= 0
    # assert wallet[quote] >= 0

    return wallet
