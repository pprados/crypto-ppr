"""
Agent pour distribuer les events websocket aux autres agents
"""
from asyncio import TimeoutError, get_running_loop, QueueEmpty
import logging
from asyncio import sleep, Queue
from typing import Callable, Dict, Any, List

from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException

from TypingClient import TypingClient

_call_back: List[Callable[[Dict[str, Any], Any], None]] = []


def add_user_socket(cb: Callable[[Dict[str, Any], Any], None]):
    """ La callback doit être rapide et non bloquante """
    _call_back.append(cb)


async def bot(client: TypingClient,
              client_account:Dict[str,Any],
              bot_name: str,
              agent_queues: List[Dict[str, Queue]],
              conf: Dict[str, Any]):
    log = logging.getLogger(bot_name)
    socket_manager = client.getBinanceSocketManager()

    input_queue = agent_queues[bot_name]  # Queue to receive msg for user or other agent
    await sleep(1)  # Time for waiting the initialisation of others agents
    # and start to listen
    loop = get_running_loop()
    ms = socket_manager.user_socket()
    start = True
    while True:
        async with ms as mscm:
            while True:
                if start:
                    # Signale a tous les autres agents, que la queue user est démarrée
                    for agent in agent_queues.values():
                        agent.put_nowait(
                            {
                                "from": bot_name,
                                "e": "stream_initialized"

                            })
                    start = False
                try:
                    # Reception d'ordre venant de l'API. Par exemple, ajout de fond, arret, etc.
                    msg = input_queue.get_nowait()  # FIXME: lecture de 2 queues en //
                    if msg['e'] == 'kill':
                        log.warning("Receive kill")
                        return
                except QueueEmpty:
                    pass  # Ignore

                msg = await mscm.recv()
                msg['_stream']='@user'
                # await asyncio.gather([loop.create_task(cb[0](msg, cb[1])) for cb in _call_back])
                for cb in _call_back:
                    await cb(msg)
