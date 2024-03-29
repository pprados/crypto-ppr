import logging
import re
from asyncio import TimeoutError
from datetime import datetime
from decimal import Decimal
from random import randint
from typing import Any, Tuple, Dict, Union, Optional, Callable

import jstyleson as json
from binance import AsyncClient
from binance.enums import *
from binance.exceptions import BinanceAPIException

from conf import CHECK_RESILIENCE
from shared_time import get_now, ts_to_str

Order_attr = Union[str, float, Decimal, int, bool]
Order = Dict[str, Order_attr]
Wallet = Dict[str, Decimal]

if CHECK_RESILIENCE:
    rescue_period = datetime.now().timestamp()

log = logging.getLogger(__name__)


async def anext(ait):
    global rescue_period
    if CHECK_RESILIENCE and randint(0, 100) < CHECK_RESILIENCE:
        x = datetime.now().timestamp()
        if x - rescue_period > 2:
            rescue_period = x
            raise TimeoutError("Fake timeout")

    return await ait.__anext__()


async def benefice(engine: 'Engine', log: logging, wallet: Dict[str, Decimal], initial_wallet: Dict[str, Decimal]) -> None:
    log_wallet(log, initial_wallet, prefix="Initial:")
    log_wallet(log, wallet, prefix="Current:")
    await engine.log_result(log, wallet, initial_wallet)


def _parse_order(order: Dict[str, Any]) -> Tuple[str, str, Decimal, Decimal]:
    base, quote = split_symbol(order['symbol'])
    side = order['side']
    quantity = None
    quote_order_qty = None
    price = None
    if 'price' in order:
        price = Decimal(str(order['price']))
    if 'fills' in order:
        # TODO [{'price': '2543.14000000', 'qty': '0.03932000', 'commission': '0.00000000', 'commissionAsset': 'ETH', 'tradeId': 1135}]
        fills = order['fills']
        if fills:
            price = Decimal(str(fills[0]['price']))  # FIXME
    if 'executedQty' in order:
        quantity = Decimal(str(order['executedQty']))
    if 'quantity' in order:
        quantity = Decimal(str(order['quantity']))
    if 'quoteOrderQty' in order:
        quote_order_qty = Decimal(str(order['quoteOrderQty']))
    return side, base, quote, quantity, quote_order_qty, price


def log_wallet(log: logging, wallet: Wallet) -> None:
    log.info("wallet:" + " ".join([f"{k}={v}" for k, v in wallet.items()]))


def json_dumps(obj: Any) -> str:
    cop = obj.copy()

    return json.dumps(obj, indent=2,
                      skipkeys=True,
                      default=_serialize)


def json_loads(tx) -> Dict[str, Any]:
    return json.loads(tx,
                      parse_float=Decimal,
                      )


def json_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return json_loads(json_dumps(order))


def generate_order_id(agent_name: str):
    # TODO: aléa alpha sur 20 chars
    return agent_name + "-order-" + str(randint(100000, 999999))


def generate_bot_id(bot: str):
    return bot + "-" + str(randint(100000, 999999))


def str_d(d: Decimal) -> str:
    return str(remove_exponent(d))


def update_order(wsi: Dict[str, Any], current_price: Optional[Decimal], order: Dict[str, Any], accept_upper=True) -> \
        Dict[
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
    elif order['type'] in (ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT):
        price = _adjuste_price(current_price, price, wsi)
        stopPrice = _adjuste_price(current_price, stopPrice, wsi)
    elif order['type'] in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER):
        if quantity:
            quantity = update_market_lot_size(wsi, quantity)

    if quantity:  # else 'quoteOrderQty'
        # lot
        if quantity < wsi.lot.minQty:
            quantity = wsi.lot.minQty
        if quantity > wsi.lot.maxQty:
            quantity = wsi.lot.maxQty

        if (quantity - wsi.lot.minQty) % (wsi.lot.stepSize) != 0:
            quantity = quantity - (Decimal(quantity) % (wsi.lot.stepSize))

        # MIN_NOTIONAL (https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#min_notional)
        # notional is price * quantity
        if wsi.min_notional.applyToMarket and order["type"] == ORDER_TYPE_MARKET:
            if current_price:
                while current_price * quantity < wsi.min_notional.minNotional:
                    quantity += wsi.lot.minQty
        elif order["type"] in (ORDER_TYPE_LIMIT, ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT):
            while price * quantity < wsi.min_notional.minNotional:
                quantity += wsi.lot.minQty

        if not accept_upper:
            if order["type"] in (ORDER_TYPE_LIMIT, ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_STOP_LOSS_LIMIT):
                if order['quantity'] * order['price'] < quantity * price:
                    raise ValueError("Impossible to update the price or quantity")
            elif quantity > order['quantity']:
                raise ValueError("Impossible to update the quantity")
        if 'quantity' not in order:
            assert ("BUG")
        order['quantity'] = remove_exponent(quantity)  # round_step_size(quantity, wsi.lot.stepSize)
    if "price" in order:
        order["price"] = remove_exponent(price)
    if "stopPrice" in order:
        order["stopPrice"] = remove_exponent(stopPrice)

    # dernière vérification
    check_order(wsi, current_price, order)

    return order


def _adjuste_price(current_price: Optional[Decimal], price: Decimal, wsi: Dict[str, Any]):
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
    return price.normalize()


def update_market_lot_size(wsi: Dict[str, Any], quantity: Decimal) -> Decimal:
    if quantity < wsi.market_lot_size.minQty:
        quantity = wsi.market_lot_size.minQty
    if quantity > wsi.market_lot_size.maxQty:
        quantity = wsi.market_lot_size.maxQty
    if wsi.market_lot_size.stepSize:
        if (quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize != 0:
            quantity = quantity - ((quantity - wsi.market_lot_size.minQty) % wsi.market_lot_size.stepSize)
    return quantity


def str_order(order: Dict[str, Any]):
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


_alias_symbol = {
    "BTC": "₿",
    "ETH": "Ξ",
    "USDT": "₮",
    "XRP": "✕",
    "BCH": "Ƀ",
    "BSV": "Ɓ",
    "LTC": "Ł",
    "EOS": "ε",
    "ADA": "₳",
    "XTZ": "ꜩ",
    "XMR": "ɱ",
    "ETC": "ξ",
    "MKR": "Μ",
    "ZEC": "ⓩ",
    "DOGE": "Ð",
    "REP": "Ɍ",
    "DAI": "◈",
    "STEEM": "ȿ"
}


def alias_symbol(symbol: str) -> str:
    return _alias_symbol.get(symbol, symbol)


def wallet_from_symbol(client_account, symbol):
    base, quote = split_symbol(symbol)
    balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
    balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))
    wallet = {}
    wallet[base] = balance_base["free"]
    wallet[quote] = balance_quote["free"]
    return wallet

def remove_exponent(d:Decimal) -> Decimal:
    return d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()

async def to_usdt(client: AsyncClient, log: logging, asset: str, val: Decimal) -> Decimal:
    try:
        if not val:
            return Decimal(0)
        if asset in ("USDT", "USDC", "BUSD"):
            return val
        if asset == "ETH":
            return ((await client.get_symbol_ticker(symbol="ETHUSDT"))["price"] * val)
        if asset == "BTC":
            return ((await client.get_symbol_ticker(symbol="BTCUSDT"))["price"] * val)
        if asset == "BUSD":
            return ((await client.get_symbol_ticker(symbol="BUSDUSDT"))["price"] * val)
        if asset in ["BIDR", "BRL", "BVND", "DAI", "IDRT", "NGN", "RUB", "TRY",
                     "UAH"]:  # FIXME: a tester. Inversion de la conv ?
            return (await client.get_symbol_ticker(symbol="USDT" + asset))["price"]
        return ((await client.get_symbol_ticker(symbol=asset + "USDT"))["price"] * val)
    except BinanceAPIException as ex:
        if ex.code == -1121:
            log.error(f"to_usdt impossible with {asset}")
        raise


def _str_dump_order(order: Dict[str, Any], prefix: str, suffix: str = '') -> str:
    side, token, other, quantity, quote_order_qty, price = _parse_order(order)
    stoken = alias_symbol(token)
    sother = alias_symbol(other)
    pre_suffix = ''
    if order['type'] in (ORDER_TYPE_STOP_LOSS_LIMIT, ORDER_TYPE_STOP_LOSS):
        pre_suffix = " for stop the loss"
    if order['type'] in (ORDER_TYPE_TAKE_PROFIT_LIMIT, ORDER_TYPE_TAKE_PROFIT):
        pre_suffix = " for take profit"
    str_price = "MARKET" if order['type'] in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT_MAKER) else str_d(price)
    if quantity and str_price != "MARKET":
        return f"{prefix}{side} {str_d(quantity)} {stoken} at {str_price} {sother}{pre_suffix}{suffix}"
    elif str_price == "MARKET":
        calculate_price = Decimal(order['cummulativeQuoteQty']) / Decimal(order['executedQty'])
        if 'cummulativeQuoteQty' in order:
            return f"{ts_to_str(get_now())}: {prefix}{side} {str_d(quantity)} {stoken} at {calculate_price} {sother}" \
                   f" for {quantity * calculate_price} {sother}" \
                   f" {pre_suffix}{suffix}"
        else:
            if 'quoteOrderQty' in order:
                return f"{ts_to_str(get_now())}: {prefix}{side} {stoken} for {order['quoteOrderQty']} {sother}" \
                       f" at MARKET" \
                       f" for {quantity * calculate_price} {sother}" \
                       f" {pre_suffix}{suffix}"
            else:
                return f"{ts_to_str(get_now())}: {prefix}{side} {str_d(quantity)} {stoken} at MARKET" \
                       f" for {quantity * calculate_price} {sother}" \
                       f" {pre_suffix}{suffix}"
    elif quote_order_qty:
        return f"{prefix}{side} {stoken} " \
               f"for {str_d(quote_order_qty)} {sother} at MARKET" \
               f" {pre_suffix}{suffix}"


def _dump_order(log: Callable,
                order: Dict[str, Any], prefix: str, suffix: str = ''):
    return log(_str_dump_order(order, prefix, suffix))


def log_add_order(log: logging, order: Dict[str, Any], prefix=None):
    _dump_order(log.info, order, f"Try to " if not prefix else prefix, "...")


def log_wallet(log: logging, wallet: Wallet, prefix="wallet:") -> None:
    log.info(prefix + " ".join([f"{k}:{v:+}" for k, v in wallet.items() if v]))


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


def get_order_price(order: Order):
    if "origQty" in order:
        return Decimal(order["cummulativeQuoteQty"]) / Decimal(order["origQty"])
    else:
        return Decimal(order['price'])
