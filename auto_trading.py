import logging
import os
import tracemalloc
from asyncio import Queue, sleep, wait, FIRST_COMPLETED
from asyncio import TimeoutError, get_event_loop
from decimal import Decimal, getcontext, FloatOperation
from importlib import import_module
from pathlib import Path
from typing import Dict

from aiohttp import ClientConnectorError, ClientOSError
from binance import BinanceSocketManager
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

import global_flags
from TypingClient import TypingClient
from conf import MIN_RECONNECT_WAIT
from simulate_client import EndOfDatas, SimulateFixedValues
from tools import atomic_load_json

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = os.environ.get("BINANCE_API_TEST", "false").lower() == "true"


async def main():
    log = logging.getLogger(__name__)
    client = None
    socket_manager = None
    log.info("Start auto_trading")
    finished = []
    unfinished = []
    while True:
        agents = []
        bot_queues: Dict[str, Queue] = {}
        try:
            conf, rollback = atomic_load_json(Path("conf.json"))
            if rollback:
                logging.warning("Use the rollback version of conf.json")

            # initialise the client
            # {"verify": False, "timeout": 20}
            # client = await AsyncClient.create(api_key, api_secret, testnet=test_net)
            while True:
                # FIXME
                client = await TypingClient.create(api_key, api_secret, testnet=test_net)
                # global_flags.simulation = True
                # client = await SimulateClient.create(api_key, api_secret, testnet=test_net)
                # client = await SimulateRandomValues("BTCUSDT",min=33000,max=35000,step=10)
                # client = SimulateFixedValues(
                #     client,
                #     "BTCUSDT",
                #     [
                #         Decimal(36000),
                #         Decimal(35000),
                #         Decimal(34900),
                #         Decimal(34800),
                #     ])
                socket_manager = client.getBinanceSocketManager()
                try:
                    await client.ping()
                    break
                except BinanceAPIException:
                    log.warning("Ping fail")
                    await sleep(MIN_RECONNECT_WAIT)

            # client = Client(api_key, api_secret, testnet=test_net)

            # Création des agents à partir de conf.json.
            # Le nom de l'agent correspond au module à importer. Le code dans etre dans une async fonction agent(...).
            # Ou bien,
            # Dans les paramètres de l'agent, il peut y avoir un paramètre 'function' pour indiquer la fonction à appliquer
            # sinon, c'est le nom de l'agent qui est utilisé. Cela permet d'invoquer le même agent avec plusieurs jeux de
            # parametres.
            # Le nom peut se limiter au module, ou etre complet (module or module.ma_fonction)

            # Les infos du comptes, pour savoir ce qui est gardé par les agents
            client_account = await client.get_account()
            # Agent la balance par defaut pour les agents. FIXME: Glups, s'il y a des ordres en cours...
            for balance in client_account['balances']:
                balance['agent_free'] = balance['free']

            for agent in conf:
                bot_name = list(agent.keys())[0]
                conf = agent[bot_name]
                fn_name = conf["function"] if "function" in conf else bot_name
                if '.' not in fn_name:
                    fn_name += ".bot"
                module_path, fn = fn_name.rsplit(".", 1)
                async_fun = getattr(import_module(module_path), fn)

                bot_queues[bot_name] = Queue()  # Create a queue for the bot
                print(bot_name)
                agents.append(loop.create_task(async_fun(client,
                                                         client_account,
                                                         bot_name,
                                                         bot_queues,
                                                         conf)))

            # Tous les résultats sont agrégé dans le retour du gather.
            # Donc, pas possible de capturer sans attendre les autres
            # await gather(*agents, return_exceptions=True)  # Lance tous les agents en //
            while True:
                finished, unfinished = await wait(agents, return_when=FIRST_COMPLETED)
                for task in finished:
                    task.result()  # Raise exception if error
                agents = unfinished
                if not unfinished:
                    # No more coroutine
                    return

        except (BinanceAPIException, ClientConnectorError, TimeoutError) as ex:
            # FIXME: Bug en cas de perte totale du réseau, sur la résolution DNS
            # Wait and retry
            ex_msg = str(ex)
            if not ex_msg or ex_msg == 'None':
                ex_msg = ex.__class__.__name__
            log.error(f"Binance communication error ({ex_msg})")
            for queue in bot_queues.values():
                queue.put_nowait(
                    {
                        "from": "_root",
                        "e": "kill"
                    })
            log.info(f"Sleep {MIN_RECONNECT_WAIT}s before restart...")
            await sleep(MIN_RECONNECT_WAIT)
            for task in unfinished:
                task.cancel()
            log.info("Try to restart")
        finally:
            if client:
                await client.close_connection()
        if global_flags.simulation:
            break


if __name__ == "__main__":
    ctx = getcontext()
    ctx.prec = 8
    ctx.traps[FloatOperation] = True
    while True:
        try:
            load_dotenv()
            if os.environ.get("DEBUG", "false") == "true":
                tracemalloc.start()
                logging.basicConfig(level=logging.DEBUG)
            else:
                logging.basicConfig(level=logging.INFO)

            # create file handler which logs even debug messages
            fh = logging.FileHandler('ctx/auto_trading.log')
            fh.setLevel(logging.INFO)
            # create formatter and add it to the handlers
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            fh.setFormatter(formatter)
            # add the handlers to the root logger
            logging.getLogger().addHandler(fh)

            getcontext().prec = 20
            loop = get_event_loop()
            loop.run_until_complete(main())
        except ClientOSError as ex:
            logging.info("Connect reset by peer")
        except EndOfDatas as ex:
            logging.info(f"Simulation ended ({ex.price} {ex.dev})")
            break
        except KeyboardInterrupt as ex:
            logging.info("Quit by user")
            break
