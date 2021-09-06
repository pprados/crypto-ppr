from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Union

from binance.enums import ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT
from binance.helpers import interval_to_milliseconds

MARKET = ORDER_TYPE_MARKET
LIMIT = ORDER_TYPE_LIMIT
COND_LIMIT_ORDER = "COND_LIMIT_ORDER"
COND_MARKET_ORDER = "COND_MARKET_ORDER"


@dataclass(init=False)
class SmartTradeParameters:
    symbol: str

    type:str    # TODO
    level:int   # TODO

    unit: Optional[Decimal]
    quote_qty: Optional[Decimal]
    size: Optional[Decimal]
    total: Optional[Decimal]

    mode: str  # "LIMIT", "MARKET", "COND_LIMIT_ORDER", "COND_MARKET_ORDER"
    cond_price: Optional[Decimal]
    price: Optional[Decimal]
    trailing_buy: Optional[Decimal]

    use_take_profit: Optional[bool]
    take_profit_base: Optional[str]
    take_profit_mode: Optional[str]
    take_profit_mode_sell:Optional[Decimal]
    take_profile_sell_timeout:Optional[int]
    take_profile_price:Optional[Decimal]
    take_profit_minimal: Optional[Decimal]
    take_profit_timeout: Optional[int]

    use_stop_loss: Optional[bool]
    stop_loss_base: Optional[str]
    stop_loss_mode: Optional[str]  # "MARKET" "COND_LIMIT_ORDER"
    stop_loss_mode_sell: Optional[Decimal]
    stop_loss_sell_timeout:Optional[int]
    stop_loss_price:Optional[Decimal]
    stop_loss_timeout:Optional[int]
    stop_loss_trailing: Optional[bool]

    def __init__(self):
        pass

def _time_to_second(val:Union[str,int]):
    if isinstance(val,int):
        return val
    else:
        return interval_to_milliseconds(val)/1000

def parse_conf(conf: Dict[str, Any]) -> SmartTradeParameters:
    params = SmartTradeParameters()

    params.symbol = conf["symbol"]
    params.unit = Decimal(str(conf["unit"])) if "unit" in conf else None
    params.size = Decimal(conf['size'].strip('%')) / 100 if "size" in conf else None
    # TODO: total en %
    params.total = Decimal(str(conf["total"])) if "total" in conf else None
    assert params.unit or params.size or params.total, "Set 'unit', 'size' or 'total'"

    params.price = Decimal(str(conf.get("price"))) if "price" in conf else None

    params.mode = conf["mode"]  # LIMIT, MARKET, cond_limit_order, cond_market_order
    assert params.mode in [MARKET, LIMIT, COND_MARKET_ORDER, COND_LIMIT_ORDER], "Invalide 'mode'"
    params.cond_price = Decimal(str(conf["cond_price"])) if "cond_price" in conf else None
    params.price = Decimal(str(conf["price"])) if "price" in conf else None
    assert params.mode not in [COND_MARKET_ORDER, COND_LIMIT_ORDER] or params.cond_price, "Set 'cond_price'"
    assert params.mode in (MARKET, COND_MARKET_ORDER) or params.price, "Set 'price'"
    params.trailing_buy = Decimal(conf['trailing'].strip('%')) / 100 if 'trailing' in conf else None
    assert not params.mode in (COND_LIMIT_ORDER, COND_MARKET_ORDER) or not params.trailing_buy
    # assert not params.total or not params.mode == MARKET

    # TAKE PROFIT
    params.use_take_profit = "take_profit" in conf
    if params.use_take_profit:
        take_profit_conf: Dict[str, Any] = conf["take_profit"]
        params.take_profit_mode = take_profit_conf["mode"]
        assert params.take_profit_mode in (MARKET, LIMIT, COND_MARKET_ORDER, COND_LIMIT_ORDER)
        params.take_profit_mode_sell = take_profit_conf.get("mode_sell", MARKET)
        if '%' in params.take_profit_mode_sell:
            # Utilisation de limit, avec un delta
            params.take_profit_mode_sell_percent = Decimal(params.take_profit_mode_sell.strip('%')) / 100
            params.take_profit_mode_sell = LIMIT
        else:
            params.take_profit_mode_sell_percent = 0
        params.take_profit_sell_timeout = _time_to_second(take_profit_conf.get("sell_timeout", 0))

        params.take_profit_base = take_profit_conf["base"]
        params.take_profit_limit_percent = None
        params.take_profit_limit = None
        l = take_profit_conf.get("price")
        if isinstance(l, str) and '%' in l:
            params.take_profit_limit_percent = Decimal(l.strip('%')) / 100
            assert params.take_profit_limit_percent >= 0
        else:
            params.take_profit_limit = Decimal(str(l))
        assert params.take_profit_base or params.take_profit_mode == LIMIT
        assert params.take_profit_limit_percent or params.take_profit_limit
        # TODO: split target
        params.take_profit_trailing = Decimal(take_profit_conf['trailing'].strip('%')) / 100 if 'trailing' \
                                                                                                in take_profit_conf else None
        assert not params.trailing_buy or params.take_profit_limit_percent or params.take_profit_limit

        params.minimal = Decimal(take_profit_conf['minimal'].strip('%')) / 100 \
            if 'minimal' in take_profit_conf else None
        assert not params.minimal or params.minimal > 0
        if params.minimal:
            params.minimal_timeout = _time_to_second(take_profit_conf['timeout'])
        params.mtp_trailing = take_profit_conf.get('trailing_minimal')

    # STOP LOST
    params.use_stop_loss = "stop_loss" in conf
    if params.use_stop_loss:
        stop_loss_conf: Dict[str, Any] = conf["stop_loss"]
        params.stop_loss_mode = stop_loss_conf.get("mode", "ask")  # "cond_limit", "market"
        assert params.stop_loss_mode in [MARKET, COND_LIMIT_ORDER]
        params.stop_loss_mode_sell = stop_loss_conf.get("mode_sell", MARKET)
        if '%' in params.stop_loss_mode_sell:
            # Utilisation de limit, avec un delta
            params.stop_loss_mode_sell_percent = Decimal(params.stop_loss_mode_sell.strip('%')) / 100
            params.stop_loss_mode_sell = LIMIT
        else:
            params.stop_loss_mode_sell_percent = 0
        params.stop_loss_sell_timeout = _time_to_second(stop_loss_conf.get("sell_timeout", 0))

        params.stop_loss_base = stop_loss_conf["base"]
        l = stop_loss_conf.get("price")
        assert l
        params.stop_loss_limit = None
        params.stop_loss_percent = None
        if isinstance(l, str) and '%' in l:
            params.stop_loss_percent = Decimal(l.strip('%')) / 100
            assert params.stop_loss_percent <= 0  # Pour un BUY
        else:
            params.stop_loss_limit = Decimal(str(l))
        # TODO: Verifier la coérance du prix, via un check order ?
        params.stop_loss_order_price = Decimal(
            str(stop_loss_conf["order_price"])) if "order_price" in stop_loss_conf else None
        assert not params.stop_loss_order_price or params.stop_loss_mode == COND_LIMIT_ORDER
        params.stop_loss_timeout = _time_to_second(stop_loss_conf.get("timeout", 0))
        params.stop_loss_trailing = stop_loss_conf.get("trailing")
        if params.use_take_profit and \
                params.take_profit_trailing and \
                params.take_profit_trailing < 0 and \
                params.take_profit_limit_percent:
            assert -params.take_profit_trailing < params.take_profit_limit_percent
    # TODO: leverage
    return params
