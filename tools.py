import json
import re
from asyncio import Queue
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from random import randint
from typing import Any, Tuple, Union, Dict, Optional

from binance.enums import ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET


def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        return float(obj)

    return obj.__dict__


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=_serialize)


def atomic_save_json(obj: Any, filename: Path) -> None:
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    with open(new_filename, "w") as f:
        json.dump(obj, f, default=_serialize)
    if filename.exists():
        filename.rename(old_filename)
    new_filename.rename(filename)
    old_filename.unlink(missing_ok=True)


def atomic_load_json(filename: Path) -> Tuple[Any, bool]:
    rollback = False
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    if new_filename.exists():
        # Try to load the new filename
        try:
            with open(new_filename) as f:
                obj = json.load(f)
            filename.unlink(missing_ok=True)
            old_filename.unlink(missing_ok=True)
            new_filename.rename(filename)
            return obj, False
        except JSONDecodeError as e:
            # new filename is dirty
            new_filename.unlink()
            if old_filename.exists():
                old_filename.rename(filename)
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
    if order["type"] == ORDER_TYPE_LIMIT:
        # Ajustement éventuelle du prix
        price = order["price"]

        # price
        if price < wsi.price.minPrice:
            price = wsi.price.minPrice
        if price > wsi.price.maxPrice:
            price = wsi.price.maxPrice
        if (price - wsi.price.minPrice) % wsi.price.tickSize != 0:
            price = price - (price - wsi.price.minPrice) % wsi.price.tickSize

        # percent
        min_price = current_price * wsi.percent.multiplierDown
        max_price = current_price * wsi.percent.multiplierUp
        if price < min_price:
            price = min_price
        if price > max_price:
            price = max_price

    if 'quantity' in order:
        quantity = order['quantity']

        # lot
        if quantity < wsi.lot.minQty:
            quantity = wsi.lot.minQty
        if quantity > wsi.lot.maxQty:
            quantity = wsi.lot.maxQty

        if (Decimal(quantity) - wsi.lot.minQty) % wsi.lot.stepSize != 0:
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
            while price * quantity < wsi.min_notional.minNotional:
                quantity += wsi.lot.minQty

        check_order(wsi, current_price, price, quantity)

        if not accept_upper:
            if order["type"] == ORDER_TYPE_LIMIT:
                if order['quantity'] * order['price'] > quantity * price:
                    raise ValueError("Impossible to update the price or quantity")
            elif quantity > order['quantity']:
                raise ValueError("Impossible to update the quantity")
        order["quantity"] = quantity.normalize()
    if order["type"] == ORDER_TYPE_LIMIT:
        order["price"] = price.normalize()
    return order


def check_order(wsi: Dict[str, Any], current_price: Decimal, price: Union[Decimal, int],
                quantity: Optional[Decimal]) -> bool:
    # price
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
        assert (quantity - wsi.lot.minQty) % wsi.lot.stepSize == 0

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
    m = re.match(r'(\w+)((USDT)|(ETH)|(BTC)|(USDT))$', 'BTCETH')
    return m.group(1), m.group(2)
