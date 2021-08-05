import logging
from asyncio import sleep, wait, FIRST_COMPLETED, TimeoutError, get_event_loop, Task
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Dict, List, Any, Optional, Set

import sdnotify
from aiohttp import ClientConnectorError
from binance.exceptions import BinanceAPIException
from telethon import TelegramClient

import global_flags
from TypingClient import TypingClient
from api_key import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE
from atomic_json import atomic_load_json, atomic_save_json
from conf import MIN_RECONNECT_WAIT, SLIPPING_TIME, NO_TELEGRAM
from events_queues import EventQueues
from simulate_client import SimulateFixedValues
from tools import generate_bot_id, log_wallet, _str_dump_order, remove_exponent


class EngineMsg(Enum):
    CREATE_BOT = "create_bot"
    DEL_BOT = "del_bot"
    GET_BOT = "get_bot"


@dataclass(init=False)
class Engine:
    api_key: str
    api_secret: str
    test_net: bool
    bots: List[Task]

    def __init__(self,
                 api_key: str,
                 api_secret: str,
                 test_net: bool,
                 simulate: bool,
                 path: Optional[Path] = None,
                 ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.test_net = test_net
        self.simulate = simulate

        self.engine_conf = []
        self.dict_engine_conf = {}

        self.log = logging.getLogger(__name__)
        self.path_conf = path if path else Path("ctx/engine.orig.json")

        self.engine_conf, rollback = atomic_load_json(self.path_conf)
        if rollback:
            logging.warning("Use the rollback version of conf.json")
        if not self.engine_conf:
            self.engine_conf = []  # Bot dans l'ordre inverse d'insertion
        if not NO_TELEGRAM:
            self._telegram = TelegramClient('auto-trading', TELEGRAM_API_ID, TELEGRAM_API_HASH)

    async def init(self):
        """ Constructeur asynchrone """
        if not NO_TELEGRAM:
            await self._init_telegram()
        loop = get_event_loop()
        self._engine_thread = loop.create_task(self.run())  # Démarre le thread pour le moteur

    async def _init_telegram(self):
        await self._telegram.connect()
        if not await self._telegram.is_user_authorized():
            self._telegram.send_code_request(TELEGRAM_PHONE)
            self._telegram.sign_in(TELEGRAM_PHONE, input('Enter the code: '))

    async def send_telegram(self, log, message: str):
        log.warning(message)
        if not NO_TELEGRAM:
            await self._telegram.send_message(TELEGRAM_PHONE, message)

    async def log_order(self, log, order: Dict[str, Any], prefix: str = "****** ", suffix: str = ''):
        await self.send_telegram(log, _str_dump_order(order, prefix, suffix))

    async def log_result(self, log, wallet: Dict[str, Decimal], initial_wallet: Dict[str, Decimal]):
        log_wallet(log,wallet,"Wallet:")
        diff = [f"{k}:{remove_exponent(v - initial_wallet[k]):+}" for k, v in wallet.items()]
        message = f"###### Result: {', '.join(diff)}"
        await self.send_telegram(log, message)

    def __del(self):
        self._engine_thread.cancel()

    async def _wait_started(self):
        while 'client' not in self.__dict__:
            await sleep(1)  # FIXME: limit de boucle

    async def _init_client(self):
        """
        Construction d'un client
        ou d'une simulation de client Binance
        """
        # initialise the client
        # {"verify": False, "timeout": 20}
        # client = await AsyncClient.create(api_key, api_secret, testnet=test_net)
        client = await TypingClient.create(
            self.api_key,
            self.api_secret, testnet=self.test_net,
        )
        if self.simulate:
            # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
            # client = await SimulateRandomValues("BTCUSDT",min=33000,max=35000,step=10)
            client = SimulateFixedValues(
                client,
                "BTCUSDT",
                [
                    Decimal(36000),
                    Decimal(35000),
                    Decimal(34900),
                    Decimal(34800),
                ])

        # client = Client(api_key, api_secret, testnet=test_net)

        return client

    # async def create_bot(self,id:str,conf:Dict[str,Any]):
    #     self.request_queue.put_nowait(
    #         {
    #             "m": EngineMsg.CREATE_BOT,
    #             "id": id,
    #             "conf": conf
    #         })
    #     result = await self.response_queue.get()
    #     if 'e' in result:
    #         raise HTTPException(status_code=result["e"], detail=result["detail"])
    #     return result
    #

    async def recreate_bot(self,
                           id: Optional[str],
                           conf: Dict[str, Any]
                           ) -> None:
        loop = get_event_loop()
        fn_name = conf["bot"]
        if not id:
            id = generate_bot_id(fn_name)
        if '.' not in fn_name:
            fn_name += ".bot"
        module_path, fn = fn_name.rsplit(".", 1)
        try:
            # Par defaut, les bots sont dans le packages bots
            module = import_module("bots." + module_path)
        except ModuleNotFoundError:
            module = import_module(module_path)

        async_fun = getattr(module, fn)
        self.event_queues.add_queue(id)
        task = loop.create_task(async_fun(self.client,
                                          self.client_account,
                                          id,
                                          self,
                                          conf))
        task.set_name(id)
        self.bots.add(task)

    async def create_bot(
            self,
            id: Optional[str],
            conf: Dict[str, Any],
    ) -> str:
        """
        Creation d'un bot avec un id
        :param id:
        :param conf:
        :return:
        """
        if not id:
            id = generate_bot_id(conf['bot'])
        if id in self.dict_engine_conf:
            raise ValueError(f"Error:Bot {id} exist")
        await self._wait_started()

        await self.recreate_bot(id, conf)
        self.engine_conf.insert(0, {id: conf})
        self.dict_engine_conf[id] = self.engine_conf[0]
        atomic_save_json(self.engine_conf, self.path_conf,
                         comment="Dans l'ordre, plus récent en premier")
        return id

    async def delete_bot(self, id: str):
        if id not in self.dict_engine_conf:
            raise ValueError(f"Error:Bot {id} not found")
        self.engine_conf.remove(self.dict_engine_conf[id])
        # Informe le bot
        self.event_queues[id].put_nowait(  # FIXME: le bot doit le capture
            {
                "stream": "@bot",
                "e": "kill"
            }
        )
        Path("ctx", id + "json").unlink(missing_ok=True)
        atomic_save_json(self.engine_conf, self.path_conf)

    @staticmethod
    def _resume(id: str, bot_conf: Dict[str, Any]):
        bot = bot_conf[next(iter(bot_conf))]
        state = atomic_load_json(Path("ctx", id + ".json"))[0]
        if state:
            return {"id": id,
                    "bot": bot['bot'],
                    "state": state['state'],
                    "bot_start": int(state['bot_start']),
                    "bot_stop": int(state['bot_stop']) if state['bot_stop'] else None
                    }
        else:
            return {}

    async def list_bot_id(self):
        return [Engine._resume(k, self.dict_engine_conf[k]) for k in self.dict_engine_conf.keys()]

    async def get_bot(self, id: str):
        if id not in self.dict_engine_conf:
            raise ValueError(f"Error:Bot {id} not found")
        return \
            {
                "id": id,
                "conf": self.dict_engine_conf[id],
                "state": atomic_load_json(Path("ctx", id + ".json"))[0]
            }

    async def run(self):
        loop = get_event_loop()
        log = self.log
        log.info("Start auto_trading")
        await self.send_telegram(log, "Auto-trading started")

        n = sdnotify.SystemdNotifier()
        n.notify("READY=1")  # Informe SystemD

        while True:
            self.bots: Set[Task] = set()
            client = None
            try:

                client = await self._init_client()  # Reset client

                # Création des agents à partir de conf.json.

                # Les infos du comptes, pour savoir ce qui est gardé par les agents
                self.client_account: Dict[str, Any] = await client.get_account()
                # Calcule la balance par defaut pour les agents. FIXME: Glups, s'il y a des ordres en cours...
                for balance in self.client_account['balances']:
                    balance['agent_free'] = balance['free']

                full_wallet = {x['asset']: x['free'] for x in self.client_account['balances']}
                log_wallet(log, full_wallet)

                # Regroupement de tous les évenments binance dans une queue unique, broadcasté vers les queues des bots
                self.event_queues = EventQueues(client)
                # FXIME: rendre la liste dynamique
                self.event_queues.add_streams(
                    [
                        # "btcusdt@aggTrade",
                        "btcusdt@trade",  # FIXME: rendre paramétrable
                        "btcusdt@bookTicker",
                        # "ethusdt@trade",
                        # "ethusdt@bookTicker",
                    ])
                self.client = client  # Commit the initialization

                # Re-création des bots en cours d'après la conf
                self.dict_engine_conf = {}  # Bot par clé pour un accès direct
                for bot_conf in self.engine_conf:
                    id = next(iter(bot_conf))
                    conf = bot_conf[id]
                    self.dict_engine_conf[id] = bot_conf
                    log.info(f"Restart bot {id}")
                    await self.recreate_bot(id, conf)

                # Boucle d'avancement des bots, et de gestion des messages de l'API
                while True:
                    if self.bots:
                        finished, unfinished = await wait(self.bots,
                                                          loop=loop,
                                                          timeout=SLIPPING_TIME,
                                                          # Ajout éventuellement de nouveaux bots
                                                          return_when=FIRST_COMPLETED)
                        for task in finished:
                            del self.event_queues[task.get_name()]
                            task.result()  # Raise exception if error
                        assert unfinished is not None
                        self.bots = unfinished
                    else:
                        await sleep(SLIPPING_TIME)  # Return to event loop
                    # if not unfinished:
                    #     # No more coroutine
                    #     return
                    n.notify("WATCHDOG=1")  # FIXME: moins souvent. Informe SystemD que je suis toujours vivant

            except (BinanceAPIException, ClientConnectorError, TimeoutError) as ex:
                # FIXME: Bug en cas de perte totale du réseau, sur la résolution DNS
                # Wait and retry
                if isinstance(ex, BinanceAPIException) and ex.code == 0 \
                        and ex.message.startswith('Invalid JSON error message from Binance'):
                    log.error(ex.message)
                ex_msg = str(ex)
                if not ex_msg or ex_msg == 'None':
                    ex_msg = ex.__class__.__name__
                log.error(f"Binance communication error ({ex_msg})")
                # Informe tous les bots
                self.event_queues.broadcast_msg(
                    {
                        "stream": "@bot",
                        "e": "kill"
                    })
                log.info(f"Sleep {MIN_RECONNECT_WAIT}s before restart...")
                await sleep(MIN_RECONNECT_WAIT)
                log.info("Try to restart")
            except Exception as ex:
                log.exception(ex)
                raise

            finally:
                if self.bots:
                    for b in self.bots:
                        b.cancel()
                if client:
                    await client.close_connection()
                    client = None
            if global_flags.simulate:
                break
