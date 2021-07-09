# TODO: Conditional to buy after price rises.
# Les conditions c'est si les prix montes.
# J'attend que cela dépasse une resistance, alors je place mon ordre.
import logging
import random
from asyncio import Queue, sleep
from pathlib import Path

from aiohttp import ClientConnectorError
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

import global_flags
from TypingClient import TypingClient
from atomic_json import atomic_load_json, atomic_save_json
from bot_generator import BotGenerator
from events_queues import EventQueues
from shared_time import sleep_speed, get_now
from simulate_client import EndOfDatas
from tools import log_wallet, anext, split_symbol, wallet_from_symbol
from .smart_trade import SmartTrade
from .smart_trades_conf import *


# Utilisation d'un generateur pour pouvoir utiliser la stratégie
# dans une autre.
class InfiniteBot(BotGenerator):
    # self.last_price c'est le prix de la dernière transaction

    POOLING_SLEEP = 2 * sleep_speed()

    STATE_INIT = "init"
    STATE_ADD_ST = "add_smart_trade"
    STATE_WAIT_ST = "wait_smart_trade"

    STATE_ERROR = BotGenerator.STATE_ERROR

    def is_finished(self):
        return self.state == InfiniteBot.STATE_FINISHED

    # TODO: cancel

    # async def _start(self,
    #                  client: TypingClient,
    #                  event_queues: EventQueues,
    #                  queue: Queue,
    #                  log: logging,
    #                  init: Dict[str, str],
    #                  **kwargs) -> 'FullBot':
    #     self._generator = self.generator(client,
    #                                      event_queues,
    #                                      queue,
    #                                      log,
    #                                      init,
    #                                      **kwargs)
    #     await self.next()
    #     return self

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

        try:
            await sleep(random.randint(0, 5))
            if not init:
                # Premier départ
                now = get_now()
                init = {
                    "bot_start": now,
                    "bot_last_update": now,
                    "bot_stop": None,
                    "running": True,
                    "state": InfiniteBot.STATE_INIT,
                    "smart_trade": None,
                    "wallet": {},
                }
            self.update(init)
            del init
            params = conf
            symbol = params['st_conf']['symbol']

            # ---- Initialisation du bot
            base, quote = split_symbol(symbol)
            self.wallet = wallet_from_symbol(client_account, symbol)
            self.initial_wallet = self.wallet.copy()

            # Récupération des paramètres
            params = conf

            # ----------------- Reprise du context des sous-generateor après un crash ou un reboot
            if not self:
                # Premier démarrage
                log.info(f"Started")
                base, quote = split_symbol(params.symbol)
                balance_base = next(filter(lambda x: x['asset'] == base, client_account['balances']))
                balance_quote = next(filter(lambda x: x['asset'] == quote, client_account['balances']))
                self.wallet[base] = balance_base["free"]
                self.wallet[quote] = balance_quote["free"]
                self.initial_wallet = self.wallet.copy()
            else:
                # Reset les sous generateur
                if 'smart_trade' in self and self.smart_trade:
                    self.smart_trade = await SmartTrade.create(
                        client,
                        engine,
                        queue,
                        log,
                        self.smart_trade,
                        generator_name="testing",
                        client_account=client_account,
                        conf=params['st_conf']
                    )

            # Finite state machine
            # C'est ici que le bot travaille sans fin, en sauvant sont état à chaque transition
            yield self

            while True:
                # try:
                #     # Reception d'ordres venant de l'API. Par exemple, ajout de fond, arrêt, etc.
                #     msg = bot_queue.get_nowait()
                #     if msg['e'] == 'kill':
                #         await engine.send_telegram(log,"Receive kill")
                #         return
                # except QueueEmpty:
                #     pass  # Ignore

                if self.state == InfiniteBot.STATE_INIT:
                    self.state = InfiniteBot.STATE_ADD_ST

                elif self.state == InfiniteBot.STATE_ADD_ST:
                    await engine.send_telegram(log,"****** New Smarttrade")
                    self.smart_trade = await SmartTrade.create(  # FIXME: sous-bot paramétrable
                        client,
                        engine,
                        queue,
                        log,
                        generator_name="testing",
                        client_account=client_account,
                        conf=params['st_conf'],
                    )
                    self.state = InfiniteBot.STATE_WAIT_ST
                    yield self
                elif self.state == InfiniteBot.STATE_WAIT_ST:
                    await anext(self.smart_trade)
                    if self.smart_trade.is_finished():
                        await sleep(1)
                        self.state = InfiniteBot.STATE_ADD_ST
                    if self.smart_trade.is_error():
                        log.error("Smart trade in error")
                        self._set_state_error()
                    yield self
                elif self.state == SmartTrade.STATE_ERROR:
                    self._set_state_error()
                    return
                else:
                    log.error(f'Unknown state \'{self["state"]}\'')
                    self._set_state_error()
                    yield self
                    return


        except BinanceAPIException as ex:
            if ex.code in (-1013, -2010) and ex.message.startswith("Filter failure:"):
                log.error(ex.message)
                self._set_state_error()
                yield self
            elif ex.code == -2011 and ex.message == "Unknown order sent.":
                log.error(ex.message)
                log.error(self.buy_order.order)
                log.exception(ex)
                self._set_state_error()
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request is outside of the recvWindow.':
                log.error(ex.message)
                self._set_state_error()
                yield self
            elif ex.code == -1021 and ex.message == 'Timestamp for this request was 1000ms ahead of the server\'s time.':
                log.error(ex.message)
                self._set_state_error()
                yield self
            else:
                log.exception("Unknown error")
                log.exception(ex)
                self._set_state_error()
                yield self

        # except (ClientConnectorError, asyncio.TimeoutError, aiohttp.ClientOSError) as ex:
        #     # Attention, pas de sauvegarde.
        #     log.exception(ex)
        #     raise ex


# Bot qui utilise le generateur correspondant
# et se charge de sauver le context.
async def bot(client: TypingClient,
              client_account: Dict[str, Any],
              bot_name: str,
              engine: 'Engine',
              conf: Dict[str, Any]):
    path = Path("ctx", bot_name + ".json")

    log = logging.getLogger(bot_name)
    bot_queue = engine.event_queues[bot_name]

    # Lecture éventuelle du context sauvegardé
    state_for_generator = {}
    if not global_flags.simulate and path.exists():
        state_for_generator, rollback = atomic_load_json(path)
        assert not rollback
        log.info(f"Restart with state={state_for_generator['state']}")

    # Puis initialisation du generateur
    bot_generator = await InfiniteBot.create(client,
                                             engine,
                                             bot_queue,
                                             log,
                                             state_for_generator,
                                             generator_name=bot_name,
                                             client_account=client_account,
                                             conf=conf,
                                             )
    try:
        while True:
            rc = await anext(bot_generator)
            if not global_flags.simulate:
                if bot_generator.is_error():
                    raise ValueError("ERROR state not saved")  # FIXME
                atomic_save_json(bot_generator, path)
            if rc == bot_generator.STATE_FINISHED:
                break
    except EndOfDatas:
        log.info("######: Final result of simulation:")
        log_wallet(log, bot_generator.wallet)
        raise
    except (BinanceAPIException, ClientConnectorError, TimeoutError) as ex:
        ex_msg = str(ex)
        if not ex_msg or ex_msg == 'None':
            ex_msg = ex.__class__.__name__
        log.error(f"Binance communication error ({ex_msg})")
        raise
