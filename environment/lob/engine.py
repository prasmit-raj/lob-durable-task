"""In-memory limit order book with price-time priority.

Prices are integer ticks and quantities are integer lots. One lock guards
all state, so commands are applied one at a time in `seq` order.
"""
from __future__ import annotations

import bisect
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class Order:
    order_id: str
    client_order_id: str
    account: str
    symbol: str
    side: str  # "buy" | "sell"
    price: int
    qty: int
    remaining: int
    status: str = "open"  # "open" | "filled" | "cancelled"
    seq: int = 0


class Book:
    def __init__(self) -> None:
        self.levels: dict[str, dict[int, deque[Order]]] = {"buy": {}, "sell": {}}
        self.prices: dict[str, list[int]] = {"buy": [], "sell": []}  # ascending

    def add(self, order: Order) -> None:
        levels = self.levels[order.side]
        if order.price not in levels:
            levels[order.price] = deque()
            bisect.insort(self.prices[order.side], order.price)
        levels[order.price].append(order)

    def remove(self, order: Order) -> None:
        queue = self.levels[order.side][order.price]
        queue.remove(order)
        if not queue:
            self._drop_level(order.side, order.price)

    def best(self, side: str) -> int | None:
        prices = self.prices[side]
        if not prices:
            return None
        return prices[-1] if side == "buy" else prices[0]

    def pop_front(self, side: str, price: int) -> None:
        queue = self.levels[side][price]
        queue.popleft()
        if not queue:
            self._drop_level(side, price)

    def _drop_level(self, side: str, price: int) -> None:
        del self.levels[side][price]
        self.prices[side].remove(price)

    def depth(self) -> dict:
        def level(side: str, price: int) -> list[int]:
            return [price, sum(o.remaining for o in self.levels[side][price])]

        return {
            "bids": [level("buy", p) for p in reversed(self.prices["buy"])],
            "asks": [level("sell", p) for p in self.prices["sell"]],
        }


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.books: dict[str, Book] = defaultdict(Book)
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"position": 0, "cash": 0})
        )
        self.events: list[dict] = []

    def place(self, account: str, client_order_id: str, symbol: str,
              side: str, price: int, qty: int) -> dict:
        with self.lock:
            order = Order(uuid.uuid4().hex, client_order_id, account, symbol,
                          side, price, qty, qty)
            accepted = self._emit("accepted", order_id=order.order_id,
                                  client_order_id=client_order_id, account=account,
                                  symbol=symbol, side=side, price=price, qty=qty)
            order.seq = accepted["seq"]
            self.orders[order.order_id] = order
            fills = self._match(order)
            if order.remaining:
                self.books[symbol].add(order)
            else:
                order.status = "filled"
            return {"order": asdict(order), "fills": fills, "seq": order.seq}

    def cancel(self, order_id: str) -> dict:
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(order_id)
            if order.status != "open":
                raise Conflict(f"order is {order.status}")
            self.books[order.symbol].remove(order)
            order.status = "cancelled"
            event = self._emit("cancelled", order_id=order_id, remaining=order.remaining)
            return {"order": asdict(order), "seq": event["seq"]}

    def get_order(self, order_id: str) -> dict:
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(order_id)
            return asdict(order)

    def depth(self, symbol: str) -> dict:
        with self.lock:
            return self.books[symbol].depth() if symbol in self.books else {"bids": [], "asks": []}

    def account(self, account: str) -> dict:
        with self.lock:
            held = self.positions.get(account, {})
            return {"account": account, "positions": {s: dict(p) for s, p in held.items()}}

    def events_after(self, after: int, limit: int) -> list[dict]:
        with self.lock:
            # seq starts at 1 and has no gaps, so seq N lives at index N-1.
            return [dict(e) for e in self.events[after:after + limit]]

    def _match(self, taker: Order) -> list[dict]:
        book = self.books[taker.symbol]
        opposite = "sell" if taker.side == "buy" else "buy"
        fills = []
        while taker.remaining:
            price = book.best(opposite)
            if price is None:
                break
            if (taker.side == "buy" and price > taker.price) or \
               (taker.side == "sell" and price < taker.price):
                break
            maker = book.levels[opposite][price][0]
            qty = min(taker.remaining, maker.remaining)
            maker.remaining -= qty
            taker.remaining -= qty
            buyer, seller = (taker, maker) if taker.side == "buy" else (maker, taker)
            fills.append(self._emit(
                "fill", symbol=taker.symbol, price=price, qty=qty,
                maker_order_id=maker.order_id, taker_order_id=taker.order_id,
                buyer=buyer.account, seller=seller.account,
            ))
            self._settle(buyer.account, seller.account, taker.symbol, price, qty)
            if maker.remaining == 0:
                maker.status = "filled"
                book.pop_front(opposite, price)
        return fills

    def _settle(self, buyer: str, seller: str, symbol: str, price: int, qty: int) -> None:
        self.positions[buyer][symbol]["position"] += qty
        self.positions[buyer][symbol]["cash"] -= price * qty
        self.positions[seller][symbol]["position"] -= qty
        self.positions[seller][symbol]["cash"] += price * qty

    def _emit(self, type_: str, **fields) -> dict:
        event = {"seq": len(self.events) + 1, "ts": time.time(), "type": type_, **fields}
        self.events.append(event)
        return dict(event)
