import logging
from abc import abstractmethod
from asyncio import Queue
from typing import Dict, Any

from binance import AsyncClient


# Classe abstract servant de base aux classes en charge d'un generator.
# Une instance est compatible avec json. C'est un dictionnaire.
# Il faut ajouter une méthode _start(...) qui doit créer un attribut _generator.
from events_queues import EventQueues
from shared_time import get_now
from tools import Wallet


class BotGenerator(dict):
    FINISHED = "FINISHED"
    ERROR = "ERROR"

    @classmethod
    async def create(cls,
                     client: AsyncClient,
                     event_queues: EventQueues,
                     queue:Queue,
                     log: logging,
                     init: Dict[str, Any]={},
                     **kwargs) -> 'AddOrder':
        init.pop("_generator", None)
        bot_generator = await cls()._start(client,
                                           event_queues,
                                           queue,
                                           log, init, **kwargs)
        assert '_generator' in bot_generator.__dict__
        return bot_generator

    @classmethod
    async def reset(cls,
                     client: AsyncClient,
                     event_queues: EventQueues,
                     queue:Queue,
                     log: logging,
                     init: Dict[str, Any],
                    wallet:Wallet
                    ) -> 'AddOrder':
        init.pop("_generator", None)
        bot_generator = await cls()._start(client,
                                           event_queues,
                                           queue,
                                           log,
                                           init,
                                           wallet=wallet)
        assert '_generator' in bot_generator.__dict__
        return bot_generator

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

    async def next(self) -> str:
        try:
            await self._generator.asend(None)
            return self.state
        except StopIteration:
            return BotGenerator.FINISHED
        except StopAsyncIteration:
            return BotGenerator.FINISHED

    def _set_state_error(self):
        self.state = "ERROR"
        self.bot_stop=get_now()
        self.running=False

    def _set_terminated(self):
        self.state = BotGenerator.FINISHED
        self.bot_stop=get_now()
        self.running=False


    @abstractmethod
    async def _start(self, **kwargs):
        pass

    @abstractmethod
    async def _load(self):
        pass
