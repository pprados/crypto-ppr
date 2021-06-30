import logging
import os
import re
from asyncio import Queue
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from random import randint
from typing import Any, Tuple, Dict, Union, Optional

import jstyleson as json
from binance import AsyncClient
from binance.enums import *
from binance.exceptions import BinanceAPIException
from binance.helpers import round_step_size

from shared_time import get_now, ts_to_str

Order_attr = Union[str,float,Decimal,int,bool]
Order = Dict[str,Order_attr]
Wallet = Dict[str,Decimal]

def _parse_order(order: Dict[str, Any]) -> Tuple[str, str, Decimal, Decimal]:
    base, quote = split_symbol(order['symbol'])
    side = order['side']
    quantity = None
    quote_order_qty = None
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
    if 'quoteOrderQty' in order:
        quote_order_qty = Decimal(order['quoteOrderQty'])
    return side, base, quote, quantity, quote_order_qty, price


def log_wallet(log: logging, wallet: Wallet) -> None:
    log.info("wallet:" + " ".join([f"{k}={v}" for k, v in wallet.items()]))




def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        return float(str_d(obj))

    # Detecte les generators pour ne pas les sauvers
    if hasattr(obj,'ag_frame'):
        return '_generator'
    return obj.__dict__

def json_dumps(obj: Any) -> str:
    cop = obj.copy()

    return json.dumps(obj, indent=2,
                      skipkeys=True,
                      default=_serialize)


def json_loads(tx) -> Dict[str,Any]:
    return json.loads(tx,
                      parse_float=Decimal,
                      )
def json_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return json_loads(json_dumps(order))


def atomic_save_json(obj: Any, filename: Path) -> None:
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    with open(new_filename, "w") as f:
        json.dump(obj, f,
                  default=_serialize,
                  skipkeys=True,
                  indent=2)
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
                obj = json.load(
                    f,
                    parse_float=Decimal,
                )  # Try to parse
            filename.unlink(missing_ok=True)
            old_filename.unlink(missing_ok=True)
            new_filename.rename(filename)
            os.sync()
            return obj, False
        except JSONDecodeError as e:
            # new filename is dirty
            new_filename.unlink()
            if old_filename.exists():
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
        return json.load(f,
                         parse_float=Decimal
                         ), rollback


def generate_order_id(agent_name: str):
    # TODO: aléa alpha sur 20 chars
    return agent_name + "-" + str(randint(100000, 999999))


async def wait_queue_init(input_queue: Queue) -> None:
    """ Attent l'initiatilisation des queues user et multiplexe """
    user_queue_initilized = False
    multiplex_queue_initilized = False
    while not user_queue_initilized or not multiplex_queue_initilized:
        msg = await input_queue.get()
        if msg["from"] == "stream_user" and msg["e"] == "stream_initialized":
            user_queue_initilized = True
        if msg["from"] == "stream_multiplex" and msg["e"] == "stream_initialized":
            multiplex_queue_initilized = True

def str_d(d:Decimal) -> str:
    s=f"{d:.20f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

def update_order(wsi: Dict[str, Any], current_price: Optional[Decimal], order: Dict[str, Any], accept_upper=True) -> Dict[
    str, Any]:
    """ Ajute l'ordre pour être conforme aux contraintes de Binance.
     :param accept_upper True if accept to pay little more price. Else raise an exception.
     """
    side, token, other, quantity, quote_order_qty, price = _parse_order(order)
    stopPrice = Decimal(0)
    if 'stopPrice' in order:
        stopPrice = Decimal(order['stopPrice'])
    assert 'quantity' in order or 'quoteOrderQty' in order
    if price and order["type"] == ORDER_TYPE_LIMIT:
        price = _adjuste_price(current_price, price, wsi)
    elif order['type'] in (ORDER_TYPE_TAKE_PROFIT_LIMIT,ORDER_TYPE_STOP_LOSS_LIMIT):
        price = _adjuste_price(current_price,price,wsi)
        stopPrice = _adjuste_price(current_price,stopPrice,wsi)
    elif order['type'] in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER):
        if quantity:
            quantity = update_market_lot_size(wsi,quantity)

    if quantity: # else 'quoteOrderQty'
        # lot
        if quantity < wsi.lot.minQty:
            quantity = wsi.lot.minQty
        if quantity > wsi.lot.maxQty:
            quantity = wsi.lot.maxQty

        if (quantity - wsi.lot.minQty) % wsi.lot.stepSize != 0:
            quantity = quantity - ((Decimal(quantity) - wsi.lot.minQty) % wsi.lot.stepSize)

        # MIN_NOTIONAL (https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#min_notional)
        # notional is price * quantity
        if wsi.min_notional.applyToMarket and order["type"] == ORDER_TYPE_MARKET:
            if current_price:
                while current_price * quantity < wsi.min_notional.minNotional:
                    quantity += wsi.lot.minQty
        elif order["type"] in (ORDER_TYPE_LIMIT,ORDER_TYPE_TAKE_PROFIT_LIMIT,ORDER_TYPE_STOP_LOSS_LIMIT):
            while price * quantity < wsi.min_notional.minNotional:
                quantity += wsi.lot.minQty

        if not accept_upper:
            if order["type"] in (ORDER_TYPE_LIMIT,ORDER_TYPE_TAKE_PROFIT_LIMIT,ORDER_TYPE_STOP_LOSS_LIMIT):
                if order['quantity'] * order['price'] < quantity * price:
                    raise ValueError("Impossible to update the price or quantity")
            elif quantity > order['quantity']:
                raise ValueError("Impossible to update the quantity")
        if 'quantity' not in order:
            assert ("BUG")
        order['quantity'] = quantity  # round_step_size(quantity, wsi.lot.stepSize)
    if "price" in order:
        order["price"] = price
    if "stopPrice" in order:
        order["stopPrice"] = stopPrice

    # dernière vérification
    check_order(wsi, current_price, order)

    return order


def _adjuste_price(current_price: Optional[Decimal], price:Decimal, wsi:Dict[str, Any]):
    # Ajustement éventuelle du prix
    if price < wsi.price.minPrice:
        price = wsi.price.minPrice
    if price > wsi.price.maxPrice:
        price = wsi.price.maxPrice
    if (price - wsi.price.minPrice) % wsi.price.tickSize != 0:
        price = price - (price - wsi.price.minPrice) % wsi.price.tickSize
    assert (price - wsi.price.minPrice) % wsi.price.tickSize == 0
    # percent
    if current_price:
        min_price = current_price * wsi.percent.multiplierDown
        max_price = current_price * wsi.percent.multiplierUp
        if price < min_price:
            price = min_price
        if price > max_price:
            price = max_price
    return price


def update_market_lot_size(wsi:Dict[str, Any],quantity:Decimal) -> Decimal:
    if quantity < wsi.market_lot_size.minQty:
        quantity = wsi.market_lot_size.minQty
    if quantity > wsi.market_lot_size.maxQty:
        quantity = wsi.market_lot_size.maxQty
    if wsi.market_lot_size.stepSize:
        if (quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize != 0:
            quantity = quantity - ((quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize)
    return quantity


def str_order(order:Dict[str,Any]):
    if "quantity" in order:
        order["quantity"] = str_d(order["quantity"])
    if "quoteOrderQty" in order:
        order["quoteOrderQty"] = str_d(order["quoteOrderQty"])
    if "price" in order:
        order["price"] = str_d(order["price"])
    if "stopPrice" in order:
        order["stopPrice"] = str_d(order["stopPrice"])
    return order


def check_order(wsi: Dict[str, Any], current_price: Decimal, order) -> bool:
    side, token, other, quantity, quote_order_qty, price = _parse_order(order)
    assert quantity or quote_order_qty
    # price
    if price:
        assert price >= wsi.price.minPrice
        assert price <= wsi.price.maxPrice
        assert price % wsi.price.tickSize == 0

        # percent
        if current_price:
            min_price = current_price * wsi.percent.multiplierDown
            max_price = current_price * wsi.percent.multiplierUp
            assert min_price <= price <= max_price
    elif order['type'] in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER):
            # Market lot size
            if quantity:
                assert quantity >= wsi.market_lot_size.minQty
                assert quantity <= wsi.market_lot_size.maxQty
                assert not wsi.market_lot_size.stepSize or \
                       quantity % wsi.market_lot_size.stepSize == 0

    if quantity:
        # lot
        assert quantity >= wsi.lot.minQty
        assert quantity <= wsi.lot.maxQty
        assert quantity % wsi.lot.stepSize == 0

        # TODO ICEBERG_PARTS (local, cas rare)

    # MIN_NOTIONAL
    if price and quantity:
        assert price * quantity >= wsi.min_notional.minNotional

    # TODO Max num order (global account)
    # TODO MAX_NUM_ALGO_ORDERS (global account)
    # MAX_NUM_ICEBERG_ORDERS (achat/vente de gros lot en petit morceau pour le cacher)
    # MAX_POSITION FILTER (global account)
    # EXCHANGE_MAX_NUM_ORDERS (global account)
    # EXCHANGE_MAX_NUM_ALGO_ORDERS (global account)
    return True


def split_symbol(symbol: str) -> Tuple[str, str]:
    m = re.match(r'(\w+)((USDT)|(ETH)|(BTC)|(USDC)|(BUSD)|(BNB))$', symbol)
    return m.group(1), m.group(2)

async def to_usdt(client:AsyncClient,log:logging, asset:str,val:Decimal) -> Decimal:
    try:
        if not val:
            return Decimal(0)
        if asset in ("USDT","USDC","BUSD"):
            return val
        if asset == "ETH":
            return ((await client.get_symbol_ticker(symbol="ETHUSDT"))["price"] * val)
        if asset == "BTC":
            return ((await client.get_symbol_ticker(symbol="BTCUSDT"))["price"] * val)
        if asset == "BUSD":
            return ((await client.get_symbol_ticker(symbol="BUSDUSDT"))["price"] * val)
        if asset in ["BIDR","BRL","BVND","DAI","IDRT","NGN","RUB","TRY","UAH"]: # FIXME: a tester. Inversion de la conv ?
            return (await client.get_symbol_ticker(symbol="USDT"+asset))["price"]
        return ((await client.get_symbol_ticker(symbol=asset+"USDT"))["price"] * val)
    except BinanceAPIException as ex:
        if ex.code == -1121:
            log.error(f"to_usdt impossible with {asset}")
        raise


def _dump_order(log: logging, order: Dict[str, Any], prefix: str, suffix: str = ''):
    side, token, other, quantity, quote_order_qty, price = _parse_order(order)
    pre_suffix = ''
    if order['type'] in (ORDER_TYPE_STOP_LOSS_LIMIT, ORDER_TYPE_STOP_LOSS):
        pre_suffix = " for stop the loss"
    if order['type'] in (ORDER_TYPE_TAKE_PROFIT_LIMIT,ORDER_TYPE_TAKE_PROFIT):
        pre_suffix = " for take profit"
    str_price = "MARKET" if order['type'] in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER) else str_d(price)
    if quantity and str_price!="MARKET":
        log.info(f"{ts_to_str(get_now())}: {prefix}{side} {str_d(quantity)} {token} at {str_price} {other}{pre_suffix}{suffix}")
    elif str_price == "MARKET":
        if 'cummulativeQuoteQty' in order:
            calculate_price = Decimal(order['cummulativeQuoteQty'])/Decimal(order['executedQty'])
            log.info(f"{ts_to_str(get_now())}: {prefix}{side} {str_d(quantity)} {token} at {calculate_price} {other} {pre_suffix}{suffix}")
        else:
            if 'quoteOrderQty' in order:
                log.info(
                    f"{ts_to_str(get_now())}: {prefix}{side} {token} for {order['quoteOrderQty']} {other} at MARKET {pre_suffix}{suffix}")
            else:
                log.info(f"{ts_to_str(get_now())}: {prefix}{side} {str_d(quantity)} {token} at MARKET {pre_suffix}{suffix}")
    elif quote_order_qty:
        log.info(f"{prefix}{side} {token} for {str_d(quote_order_qty)} {other} at MARKET {pre_suffix}{suffix}")


def log_add_order(log: logging, order: Dict[str, Any],prefix=None):
    _dump_order(log, order, f"Try to " if not prefix else prefix, "...")


def log_order(log: logging, order: Dict[str, Any],prefix="****** "):
    _dump_order(log, order, prefix)

def log_wallet(log: logging, wallet: Wallet) -> None:
    log.info("wallet:" + " ".join([f"{k}={v}" for k, v in wallet.items()]))


def update_wallet(wallet: Dict[str, Decimal], order: Dict[str, Any]) -> None:
    """ Mise à jour du wallet"""
    base, quote = split_symbol(order['symbol'])
    side, token, other, quantity, quote_order_qty, price = _parse_order(order)
    old_wallet = wallet.copy()
    if not price or price < 0:
        # Quote_qty
        if side == SIDE_BUY:
            wallet[base] += Decimal(order["origQty"])  # FIXME
            wallet[quote] -= Decimal(order["cummulativeQuoteQty"])
        else:
            wallet[base] -= Decimal(order["origQty"])
            wallet[quote] += Decimal(order["cummulativeQuoteQty"])
    else:
        if side == SIDE_BUY:
            wallet[base] += quantity
            wallet[quote] -= quantity * price
        else:
            wallet[base] -= quantity
            wallet[quote] += quantity * price
    assert wallet[base] >= 0
    assert wallet[quote] >= 0

def get_order_price(order:Order):
    if 'price' in order:
        return Decimal(order['price'])
    else:
        return Decimal(order["cummulativeQuoteQty"])/Decimal(order["origQty"])
