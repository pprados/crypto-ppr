import asyncio
from asyncio import Queue
from typing import List, Dict, Any

from binance.streams import ReconnectingWebsocket

from TypingClient import TypingClient

class EventQueues:
    def __init__(self,client:TypingClient):
        self._client = client
        self._queues:Dict[str,Queue]={}
        self._init=False

    def __delitem__(self, name):
        del self._queues[name]

    def __getitem__(self, name):
        return self._queues[name]

    def add_queue(self,name:str) -> None:
        q = Queue()
        self._queues[name]=q
        return q

    def broadcast_msg(self,msg:Dict[str,Any]) -> None:
        """ Duplique le message dans chaque queue """
        for k in self._queues.values():
            k.put_nowait(msg)

    def remove_streams(self,):
        raise NotImplementedError()  # TODO

    def add_streams(self,
                    multiplex:List[str]
                    ) -> None:
        if not self._init:
            socket_manager = self._client.get_socket_manager()

            # and start to listen
            loop = asyncio.get_running_loop()
            self._multiplex_socket = socket_manager.multiplex_socket(multiplex)

            class ReconnectHandle():
                def cancel(self):
                    print("cancel")

            self._multiplex_socket.reconnect_handle = ReconnectHandle()
            self._multiplex_socket.MIN_RECONNECT_WAIT = 1.0
            self._multiplex_socket.MAX_RECONNECTS = 100
            loop.create_task(_manage_multiplex_stream(self, self._multiplex_socket))

            self._user_socket = socket_manager.user_socket()
            self._user_socket.reconnect_handle = ReconnectHandle()
            self._user_socket.MIN_RECONNECT_WAIT = 1.0
            self._user_socket.MAX_RECONNECTS = 100
            loop.create_task(_manage_user_stream(self,self._user_socket))
        else:
            # FIXME: rendre dynamique, avec refcount
            raise NotImplementedError()


async def _manage_multiplex_stream(mixed_queue:EventQueues, socket:ReconnectingWebsocket) -> None:
    async with socket as mscm:
        while True:
            msg = await mscm.recv()
            #await asyncio.gather([loop.create_task(cb[0](msg, cb[1])) for cb in _call_back])
            assert msg
            m = msg['data']
            m['_stream'] = msg['stream']
            # print(m)
            mixed_queue.broadcast_msg(m)

async def _manage_user_stream(mixed_queue:EventQueues, socket:ReconnectingWebsocket) -> None:
    async with socket as mscm:
        while True:
            msg = await mscm.recv()
            assert msg
            msg['_stream'] = "@user"
            # print(msg)
            mixed_queue.broadcast_msg(msg)

