import logging
from abc import abstractmethod
from asyncio import Queue
from typing import Dict, Any

from binance import AsyncClient


# Classe abstract servant de base aux classes en charge d'un generator.
# Une instance est compatible avec json. C'est un dictionnaire.
# Il faut ajouter une méthode _start(...) qui doit créer un attribut _generator.

STOPPED="stopped"

class BotGenerator(dict):
    @classmethod
    async def create(cls,
                     client: AsyncClient,
                     user_queue: Queue,
                     log: logging,
                     init: Dict[str, Any]={},
                     **kwargs) -> 'AddOrder':
        init.pop("_generator", None)
        bot_generator = await cls()._start(client, user_queue, log, init, **kwargs)
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
            return STOPPED
        except StopAsyncIteration:
            return STOPPED

    @abstractmethod
    async def _start(self, **kwargs):
        pass

    @abstractmethod
    async def _load(self):
        pass
