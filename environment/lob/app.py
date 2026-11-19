from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from lob.engine import Conflict, Engine, NotFound

app = FastAPI(title="lob")
engine = Engine()


class OrderIn(BaseModel):
    account: str = Field(min_length=1, max_length=64)
    client_order_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(pattern=r"^[A-Z]{1,10}$")
    side: Literal["buy", "sell"]
    price: int = Field(gt=0)
    qty: int = Field(gt=0)


# Handlers are sync, so FastAPI runs them in a thread pool; the engine lock serialises them.
@app.post("/orders", status_code=201)
def place_order(body: OrderIn):
    return engine.place(body.account, body.client_order_id, body.symbol,
                        body.side, body.price, body.qty)


@app.delete("/orders/{order_id}")
def cancel_order(order_id: str):
    try:
        return engine.cancel(order_id)
    except NotFound:
        raise HTTPException(404, "order not found")
    except Conflict as exc:
        raise HTTPException(409, str(exc))


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    try:
        return engine.get_order(order_id)
    except NotFound:
        raise HTTPException(404, "order not found")


@app.get("/book/{symbol}")
def get_book(symbol: str):
    return engine.depth(symbol)


@app.get("/accounts/{account}")
def get_account(account: str):
    return engine.account(account)


@app.get("/events")
def get_events(after: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=5000)):
    return {"events": engine.events_after(after, limit)}


@app.get("/health")
def health():
    return {"ok": True}
