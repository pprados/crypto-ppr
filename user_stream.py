"""
Agent pour distribuer les events websocket aux autres agents
"""
import asyncio
import logging
from asyncio import sleep, Queue
from typing import Callable, Dict, Any, List

from binance import AsyncClient, BinanceSocketManager

_call_back: List[Callable[[Dict[str, Any], Any], None]] = []


def add_user_socket(cb: Callable[[Dict[str, Any], Any], None]):
    """ La callback doit être rapide et non bloquante """
    _call_back.append(cb)


async def bot(client: AsyncClient,
              client_account:Dict[str,Any],
              bot_name: str,
              agent_queues: Dict[str, Queue],
              conf: Dict[str, Any]):
    log = logging.getLogger(bot_name)
    await sleep(1)  # Time for waiting the initialisation of others agents
    # and start to listen
    loop = asyncio.get_running_loop()
    bm = BinanceSocketManager(client._delegate, user_timeout=60)
    ms = bm.user_socket()
    start = True
    while True:
        try:
            async with ms as mscm:
                while True:
                    if start:
                        # Signale a tous les autres agents, que la queue user est démarrée
                        for agent in agent_queues.values():
                            agent.put_nowait(
                                {
                                    "from": bot_name,
                                    "msg": "initialized"

                                })
                        start = False
                    msg = await mscm.recv()
                    # await asyncio.gather([loop.create_task(cb[0](msg, cb[1])) for cb in _call_back])
                    for cb in _call_back:
                        cb(msg)
        except TimeoutError as ex:
            log.exception(ex)  # FIXME: reprise sur erreur
        except Exception as ex:  # FIXME
            log.exception(ex)  # FIXME: reprise sur erreur