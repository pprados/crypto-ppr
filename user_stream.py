"""
Agent pour distribuer les events websocket aux autres agents
"""
import asyncio
from asyncio import sleep
from typing import Callable, Dict, Any, List, Tuple

from binance import AsyncClient, BinanceSocketManager

_call_back: List[Callable[[Dict[str, Any], Any], None]] = []


def add_user_socket(cb: Callable[[Dict[str, Any], Any], None]):
    """ La callback doit être rapide et non bloquante """
    _call_back.append(cb)


async def agent(client: AsyncClient, name: str, conf: Dict[str, Any]):
    await sleep(1)  # Time for waiting the initialisation of others agents
    print(_call_back)
    # and start to listen
    loop = asyncio.get_running_loop()
    bm = BinanceSocketManager(client._delegate, user_timeout=60)
    ms = bm.user_socket()
    async with ms as mscm:
        while True:
            msg = await mscm.recv()
            #await asyncio.gather([loop.create_task(cb[0](msg, cb[1])) for cb in _call_back])
            for cb in _call_back:
                await cb(msg)
