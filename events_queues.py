import asyncio
import logging
from asyncio import Queue, sleep
from random import randint
from typing import List, Dict, Any

from aiohttp import ClientConnectorError

from TypingClient import TypingClient
from binance.streams import ReconnectingWebsocket
from conf import MIN_RECONNECT_WAIT, MAX_RECONNECTS, MAX_RECONNECT_SECONDS

log = logging.getLogger(__name__)


class EventQueues:
    def __init__(self, client: TypingClient):
        self._client = client
        self._queues: Dict[str, Queue] = {}
        self._init = False

    def __delitem__(self, name):
        del self._queues[name]

    def __getitem__(self, name):
        return self._queues[name]

    def add_queue(self, name: str) -> None:
        q = Queue()
        self._queues[name] = q
        return q

    def broadcast_msg(self, msg: Dict[str, Any]) -> None:
        """ Duplique le message dans chaque queue """
        for k in self._queues.values():
            k.put_nowait(msg)

    def remove_streams(self, ):
        raise NotImplementedError()  # TODO

    def _restart(self):
        self._init = None
        self._handle_multiplex_stream.cancel()
        self._handle_user_stream.cancel()
        self.add_streams(self._multiplex)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._manage_multiplex_stream.__atexit()

    def add_streams(self,
                    multiplex: List[str],
                    KEEP_ALIVE_TIMEOUT=None) -> None:
        self._multiplex = multiplex  # FIXME: mélanger, ajouter
        if not self._init:
            socket_manager = self._client.get_socket_manager(user_timeout=KEEP_ALIVE_TIMEOUT)

            # and start to listen
            loop = asyncio.get_running_loop()
            self._multiplex_socket = socket_manager.multiplex_socket(multiplex)

            class ReconnectHandle():
                def cancel(self):
                    print("FIXME cancel")  # FIXME

            self.attempts_reconnect = 0
            self._multiplex_socket.reconnect_handle = ReconnectHandle()
            self._multiplex_socket.MIN_RECONNECT_WAIT = MIN_RECONNECT_WAIT
            self._multiplex_socket.MAX_RECONNECTS = MAX_RECONNECTS
            self._multiplex_socket.TIMEOUT = 60
            self._handle_multiplex_stream = loop.create_task(self._manage_multiplex_stream(self._multiplex_socket))

            self._user_socket = socket_manager.user_socket()
            self._user_socket.reconnect_handle = ReconnectHandle()
            self._user_socket.MIN_RECONNECT_WAIT = MIN_RECONNECT_WAIT
            self._user_socket.MAX_RECONNECTS = MAX_RECONNECTS
            self._user_socket.TIMEOUT = 60
            self._handle_user_stream = loop.create_task(self._manage_user_stream(self._user_socket))
        else:
            # FIXME: rendre dynamique, avec refcount
            raise NotImplementedError()

    async def close(self):
        pass

    async def _manage_user_stream(self, socket: ReconnectingWebsocket) -> None:
        while True:
            try:
                await sleep(0)
                async with socket as mscm:
                    self.attempts_reconnect = 0
                    while True:
                        msg = await mscm.recv()
                        assert msg
                        log.debug(f"user_stream:receive {msg}")
                        msg['_stream'] = "@user"
                        self.broadcast_msg(msg)
            except (ClientConnectorError, RuntimeError) as ex:
                self.attempts_reconnect += 1
                random_wait = randint(1, min(MAX_RECONNECT_SECONDS, 2 ** self.attempts_reconnect))
                log.debug(f"user_stream: wait {random_wait}s before _restart")
                await sleep(random_wait)
                self._restart()
            except Exception as ex:
                log.exception(ex)

    async def _manage_multiplex_stream(self, socket: ReconnectingWebsocket) -> None:
        while True:
            try:
                await sleep(0)
                async with socket as mscm:
                    while True:
                        msg = await mscm.recv()
                        # await asyncio.gather([loop.create_task(cb[0](msg, cb[1])) for cb in _call_back])
                        assert msg
                        log.debug(f"multiple_stream:receive {msg}")
                        if 'e' in msg and 'Max reconnect retries reached' == msg['m']:
                            raise RuntimeError(msg['m'])
                        m = msg['data']
                        m['_stream'] = msg['stream']
                        self.broadcast_msg(m)
            except (ClientConnectorError, RuntimeError) as ex:
                self.attempts_reconnect += 1
                random_wait = randint(1, min(MAX_RECONNECT_SECONDS, 2 ** self.attempts_reconnect))
                log.debug(f"multiplex_stream: wait {random_wait}s before _restart")
                await sleep(random_wait)
                self._restart()
            except Exception as ex:
                log.exception(ex)
