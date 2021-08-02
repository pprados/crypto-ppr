import logging
from abc import abstractmethod
from asyncio import Queue
from typing import Dict, Any

from binance import AsyncClient

# Classe abstract servant de base aux classes en charge d'un generator.
# Une instance est compatible avec json. C'est un dictionnaire.
# Il faut ajouter une méthode _start(...) qui doit créer un attribut _generator.
from TypingClient import TypingClient
from events_queues import EventQueues
from shared_time import get_now
from tools import anext


class BotGenerator(dict):
    STATE_FINISHED = "FINISHED"
    STATE_ERROR = "ERROR"

    @classmethod
    async def create(cls,
                     client: AsyncClient,
                     engine: 'Engine',
                     queue: Queue,
                     log: logging,
                     init: Dict[str, Any] = {},
                     **kwargs: Dict[str, Any]) -> 'BotGenerator':
        """
        Il n'est pas possible d'avoir un constructeur asynchrone,
        Donc on passe par une méthode 'create()'
        """
        init.pop("_generator", None)

        bot_generator = await cls()._start(
            client,
            engine,
            queue,
            log,
            init,
            kwargs)
        assert '_generator' in bot_generator.__dict__
        return bot_generator

    async def _start(self,
                     client: TypingClient,
                     engine: 'Engine',
                     queue: Queue,
                     log: logging,
                     init: Dict[str, str],
                     kwargs: Dict[str, Any]) -> 'WinterSummerBot':
        """ Invoke le generateur pour initialiser le bot """
        self._generator = self.generator(client,
                                         engine,
                                         queue,
                                         log,
                                         init,
                                         **kwargs)
        await anext(self)
        return self

    @abstractmethod
    async def generator(self,
                        client: AsyncClient,
                        engine: 'Engine',
                        queue: Queue,
                        log: logging,
                        init: Dict[str, str],  # Initial context
                        client_account: Dict[str, Any],
                        generator_name: str,
                        conf: Dict[str, Any],
                        **kwargs) -> None:
        pass

    def __repr__(self):
        return "{TODO}" # dict.__repr__(self)self.__dict__.__repr__()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

    async def __anext__(self) -> str:
        try:
            await self._generator.asend(None)
            return self.state
        except StopIteration:
            return BotGenerator.STATE_FINISHED
        except StopAsyncIteration:
            return BotGenerator.STATE_FINISHED

    def is_error(self):
        return self.state == BotGenerator.STATE_ERROR

    def is_finished(self):
        return self.state == BotGenerator.STATE_FINISHED

    def _set_state_error(self):
        self.state = BotGenerator.STATE_ERROR
        self.bot_stop = get_now()
        self.running = False

    def _set_terminated(self):
        self.state = BotGenerator.STATE_FINISHED
        self.bot_stop = get_now()
        self.running = False
