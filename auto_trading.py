import logging
import os
from asyncio import Queue, sleep
from asyncio import TimeoutError, gather, get_event_loop
from importlib import import_module
from pathlib import Path
from typing import Dict
import decimal

from aiohttp import ClientConnectorError
from binance import BinanceSocketManager
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

from TypingClient import TypingClient
from conf import MIN_RECONNECT_WAIT
from tools import atomic_load_json

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = os.environ.get("BINANCE_API_TEST", "false").lower() == "true"



async def main():
    log = logging.getLogger(__name__)
    client = None
    log.info("Start auto_trading")
    while True:
        agents = []
        agent_queues: Dict[str, Queue] = {}
        try:
            conf, rollback = atomic_load_json(Path("conf.json"))
            if rollback:
                logging.warning("Use the rollback version of conf.json")

            while True:
                # initialise the client
                # {"verify": False, "timeout": 20}
                # client = await AsyncClient.create(api_key, api_secret, testnet=test_net)
                client = await TypingClient.create(api_key, api_secret, testnet=test_net)
                socket_manager = BinanceSocketManager(client._delegate, user_timeout=60)

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
                    agent_name = list(agent.keys())[0]
                    conf = agent[agent_name]
                    fn_name = conf["function"] if "function" in conf else agent_name
                    if '.' not in fn_name:
                        fn_name += ".bot"
                    module_path, fn = fn_name.rsplit(".", 1)
                    async_fun = getattr(import_module(module_path), fn)

                    agent_queues[agent_name] = Queue()
                    agents.append(loop.create_task(async_fun(client,
                                                             socket_manager,
                                                             client_account,
                                                             agent_name,
                                                             agent_queues,
                                                             conf)))

                await gather(*agents, return_exceptions=True)  # Lance tous les agents en //
        except (BinanceAPIException, ClientConnectorError, TimeoutError) as ex:
            # Wait and retry
            log.exception("Binance communication error")
            for agent in agent_queues.values:
                agent.put_nowait(
                    {
                        "from": "_root",
                        "msg": "kill"
                    })
            await sleep(MIN_RECONNECT_WAIT)
            log.info("Try to reconnect")
        finally:
            if client:
                await client.close_connection()


if __name__ == "__main__":
    try:
        load_dotenv()
        decimal.getcontext().prec = 20
        logging.basicConfig(level=logging.INFO)
        loop = get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt as e:
        logging.info("Quit by user")