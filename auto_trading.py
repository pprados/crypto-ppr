#!/usr/bin/env python3
import logging
import os
import sys
import tracemalloc
from asyncio import get_event_loop, sleep
from decimal import getcontext, FloatOperation
from typing import Dict, Any, Optional

import click as click
import sdnotify
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from starlette import status

import global_flags
from api_key import api_key, api_secret, test_net
from engine import Engine
from request_json_comments import JsonCommentRoute


async def startup():
    loop = get_event_loop()

    # Creation de l'engine pour les bots. A garder dans une variable globale
    # pour que les threads associés restent en vie.
    global engine
    engine = Engine(api_key, api_secret, test_net, global_flags.simulate)
    await sleep(1)  # Wait the startup


app = FastAPI(on_startup=[startup])
app.router.route_class = JsonCommentRoute

#engine: Optional[Engine] = None  # Python 3.8
engine = None  # Python 3.7


@app.get("/bots/", status_code=status.HTTP_201_CREATED)
async def list_bot_id():
    """ List all bots Id """
    rc= await engine.list_bot_id()
    return rc


@app.post("/bots/", status_code=status.HTTP_201_CREATED)
async def create_bot(
        conf: Dict[str, Any],
        id: Optional[str] = None):
    return await engine.create_bot(id, conf)


@app.delete("/bots/{id}")
async def delete_bot(id: str):
    await engine.delete_bot(id)
    return "OK"


@app.get("/bots/{id}")
async def get_bot(id: str):
    return await engine.get_bot(id)


def init(simulate: bool):

    global_flags.simulate = simulate
    ctx = getcontext()
    ctx.prec = 8
    ctx.traps[FloatOperation] = True

    if os.environ.get("DEBUG", "false").lower() == "true":
        tracemalloc.start()
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # TODO: Voir http://www.uvicorn.org/ pour paramétrer les logs
    # TODO: https://docs.gunicorn.org/en/latest/settings.html#logging
    # create file handler which logs even debug messages
    fh = logging.FileHandler('ctx/auto_trading.log')
    fh.setLevel(logging.INFO)
    # create formatter and add it to the handlers
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    # add the handlers to the root logger
    logging.getLogger().addHandler(fh)

    getcontext().prec = 20  # Nb chiffres après la virgule


@click.command(short_help='Start bots')
@click.option("--simulate",
              help='Simulate trading',
              is_flag=True)
def main(simulate: bool):
    init(simulate)
    # TODO: Voir http://www.uvicorn.org/#running-with-gunicorn
    # TODO: voir sync worker : https://docs.gunicorn.org/en/latest/design.html#sync-workers
    # pour simplifier le run
    uvicorn.run(
        app,
        host="0.0.0.0", port=8000,
        debug=True
    )


if __name__ == "__main__":
    sys.exit(main())  # pylint: disable=no-value-for-parameter
