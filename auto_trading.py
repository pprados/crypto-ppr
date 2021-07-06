#!/usr/bin/env python3
import logging
import os
import secrets
import sys
import tracemalloc
from asyncio import get_event_loop, sleep
from decimal import getcontext, FloatOperation
from http.client import HTTPException
from typing import Dict, Any, Optional

import click as click
import uvicorn
from fastapi import FastAPI, Depends
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette import status
from starlette.responses import JSONResponse

import global_flags
from api_key import api_key, api_secret, test_net, USER, PASSWORD
from engine import Engine
from request_json_comments import JsonCommentRoute


async def startup():
    loop = get_event_loop()

    # Creation de l'engine pour les bots. A garder dans une variable globale
    # pour que les threads associés restent en vie.
    global engine
    engine = Engine(api_key, api_secret, test_net, global_flags.simulate)
    await sleep(1)  # Wait the startup


security = HTTPBasic()


def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, USER)
    correct_password = secrets.compare_digest(credentials.password, PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


app = FastAPI(on_startup=[startup],
              ocs_url=None, redoc_url=None, openapi_url=None)
app.router.route_class = JsonCommentRoute

# engine: Optional[Engine] = None  # Python 3.8
engine = None  # Python 3.7


@app.get("/openapi.json")
async def get_open_api_endpoint(_: str = Depends(get_current_username)):
    return JSONResponse(get_openapi(title="FastAPI", version=1, routes=app.routes))


@app.get("/docs")
async def get_documentation(_: str = Depends(get_current_username)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="docs")


@app.get("/bots/")
async def list_bot_id(_: str = Depends(get_current_username)):
    """ List all bots Id """
    return await engine.list_bot_id()


@app.post("/bots/", status_code=status.HTTP_201_CREATED)
async def create_bot(
        conf: Dict[str, Any],
        id: Optional[str] = None,
        _: str = Depends(get_current_username)):
    return await engine.create_bot(id, conf)


@app.delete("/bots/{id}")
async def delete_bot(id: str,
                     _: str = Depends(get_current_username)):
    await engine.delete_bot(id)
    return "OK"


@app.get("/bots/{id}")
async def get_bot(id: str,
                  _: str = Depends(get_current_username)):
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
