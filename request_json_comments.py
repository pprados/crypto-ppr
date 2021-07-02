from collections import Callable
from json import detect_encoding

import jstyleson

from fastapi import Body, FastAPI, Request, Response
from fastapi.routing import APIRoute

class JsonCommentRequest(Request):
    # Hock to accept comments in json
    async def json(self) -> bytes:
        if not hasattr(self, "_json"):
            body = await self.body()
            json_body = jstyleson.loads(body.decode(detect_encoding(body), 'surrogatepass'))
            self._json = json_body
        return self._json


class JsonCommentRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            request = JsonCommentRequest(request.scope, request.receive)
            return await original_route_handler(request)

        return custom_route_handler

