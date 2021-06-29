from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

from binance.enums import ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT

MARKET = ORDER_TYPE_MARKET
LIMIT = ORDER_TYPE_LIMIT
COND_LIMIT_ORDER = "COND_LIMIT_ORDER"
COND_MARKET_ORDER = "COND_MARKET_ORDER"


@dataclass(init=False)
class SmartTradeParameters:
    symbol: str
    unit: Optional[Decimal]
    quote_qty: Optional[Decimal]
    size: Optional[Decimal]
    total: Optional[Decimal]

    price: Decimal
    mode: str  # "limit", "market", "cond_limit_order", "cond_market_order"
    order_price: Optional[Decimal]
    training_buy: Optional[Decimal]

    use_take_profit: bool
    take_profit_mode: str
    take_profit_base: str
    take_profit_limit_percent: Optional[Decimal]
    take_profit_limit: Optional[Decimal]
    take_profit_trailing: Optional[Decimal]

    use_stop_loss: bool
    stop_loss_base: str
    stop_loss_mode: str  # "cond_limit", "market"
    stop_loss_percent: Optional[Decimal]
    stop_loss_limit: Optional[Decimal]
    stop_loss_timeout: Optional[Decimal]
    stop_loss_trailing: Optional[Decimal]

    def __init__(self):
        pass


def parse_conf(conf: Dict[str, Any]) -> SmartTradeParameters:
    params = SmartTradeParameters()

    params.symbol = conf["symbol"]
    params.unit = Decimal(conf["unit"]) if "unit" in conf else None
    params.size = Decimal(conf['size'].strip('%')) / 100 if "size" in conf else None
    # TODO: total en %
    params.total = Decimal(conf["total"]) if "total" in conf else None
    assert params.unit or params.size or params.total

    params.price = Decimal(conf.get("price")) if "price" in conf else None

    params.mode = conf["mode"]  # LIMIT, MARKET, cond_limit_order, cond_market_order
    assert params.mode in [MARKET, LIMIT, COND_MARKET_ORDER, COND_LIMIT_ORDER]
    params.order_price = Decimal(conf["order_price"]) if "order_price" in conf else None
    assert params.mode == MARKET or params.order_price
    assert params.price or params.mode == MARKET
    params.training_buy = Decimal(conf['training'].strip('%')) / 100 if 'training' in conf else None
    assert not params.mode in [COND_LIMIT_ORDER, COND_MARKET_ORDER] or not params.training_buy
    assert not params.total or not params.mode == LIMIT

    # TAKE PROFIT
    params.use_take_profit = "take_profit" in conf
    if params.use_take_profit:
        take_profit_conf: Dict[str, Any] = conf["take_profit"]
        params.take_profit_mode = take_profit_conf.get("mode","bid")
        assert params.take_profit_mode in [MARKET, LIMIT, COND_MARKET_ORDER, COND_LIMIT_ORDER]

        params.take_profit_base = take_profit_conf["base"]
        params.take_profit_limit_percent = None
        params.take_profit_limit = None
        l = take_profit_conf.get("price")
        if isinstance(l, str) and '%' in l:
            params.take_profit_limit_percent = Decimal(l.strip('%')) / 100
            assert params.take_profit_limit_percent >=0
        else:
            params.take_profit_limit = Decimal(l)
        assert params.take_profit_base or params.take_profit_mode == LIMIT
        assert params.take_profit_limit_percent or params.take_profit_limit
        # TODO: split target
        params.take_profit_trailing = Decimal(take_profit_conf['trailing'].strip('%')) / 100 if 'trailing' \
                                                                                    in take_profit_conf else None
        assert not params.training_buy or params.take_profit_limit_percent or params.take_profit_limit

    # STOP LOST
    params.use_stop_loss = "stop_loss" in conf
    if params.use_stop_loss:
        stop_loss_conf: Dict[str, Any] = conf["stop_loss"]
        params.stop_loss_mode = stop_loss_conf.get("mode","ask")  # "cond_limit", "market"
        assert params.stop_loss_mode in [MARKET, COND_LIMIT_ORDER]

        params.stop_loss_base = stop_loss_conf["base"]
        l = stop_loss_conf.get("price")
        params.stop_loss_limit=None
        params.stop_loss_percent=None
        if isinstance(l, str) and '%' in l:
            params.stop_loss_percent = Decimal(l.strip('%')) / 100
            assert params.stop_loss_percent <= 0  # Pour un BUY
        else:
            params.stop_loss_limit = Decimal(l)
        # TODO: Verifier la coérance du prix, via un check order ?
        params.stop_loss_order_price = Decimal(stop_loss_conf["order_price"]) if "order_price" in stop_loss_conf else None
        assert not params.stop_loss_order_price or params.stop_loss_mode == COND_LIMIT_ORDER
        params.stop_loss_timeout = stop_loss_conf.get("timeout",0)*1000
        params.stop_loss_trailing = stop_loss_conf.get("trailing")
    # TODO: leverage
    return params
