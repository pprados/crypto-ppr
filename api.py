from typing import Optional, Any, Dict
import uvicorn
from fastapi import FastAPI

from pydantic import BaseModel
from starlette import status

app = FastAPI()



class Order(BaseModel):
    name: str
    price: float
    is_offer: Optional[bool] = None



@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/orders/last")
async def read_last_order(order_id: int, q: Optional[str] = None):
    return {"order_id": order_id, "q": q}

@app.get("/orders/{order_id}")
async def read_order(order_id: int, q: Optional[str] = None):
    return {"order_id": order_id, "q": q}



@app.put("/orders/{order_id}")
async def update_order(order_id: int, order: Order):
    return {"order_price": order.price, "order_id": order_id}

@app.post("/orders/",status_code=status.HTTP_201_CREATED)
async def create_order(conf: Dict[str,Any],name:Optional[str]=None):
    return conf


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)