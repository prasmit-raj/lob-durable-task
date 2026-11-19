"""Durable limit order book with price-time priority.

Every command is planned against current state, written to the log, then applied.
Startup applies the same events in the same order, so live and recovered state
come from one code path (`_apply`) and match exactly, including ids and timestamps.
"""
from __future__ import annotations

import bisect
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

from lob.storage import Log


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
            del self.levels[order.side][order.price]
            self.prices[order.side].remove(order.price)

    def best_first(self, side: str) -> list[int]:
        return self.prices[side][::-1] if side == "buy" else list(self.prices[side])

    def depth(self) -> dict:
        def level(side: str, price: int) -> list[int]:
            return [price, sum(o.remaining for o in self.levels[side][price])]

        return {
            "bids": [level("buy", p) for p in reversed(self.prices["buy"])],
            "asks": [level("sell", p) for p in self.prices["sell"]],
        }


class Engine:
    def __init__(self, log: Log | None = None) -> None:
        self.lock = threading.Lock()
        self.books: dict[str, Book] = defaultdict(Book)
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"position": 0, "cash": 0})
        )
        self.events: list[dict] = []
        # (account, client_order_id) -> (order fields, response from first placement)
        self.placed: dict[tuple[str, str], tuple[tuple, dict]] = {}
        self.log = log
        if log is not None:
            for record in log.load():
                self._apply_record(record)

    def place(self, account: str, client_order_id: str, symbol: str,
              side: str, price: int, qty: int) -> dict:
        return self.submit(account, client_order_id, symbol, side, price, qty)[0]

    def submit(self, account: str, client_order_id: str, symbol: str,
               side: str, price: int, qty: int) -> tuple[dict, bool]:
        """Returns (response, created). A retry returns the original response."""
        key, fields = (account, client_order_id), (symbol, side, price, qty)
        with self.lock:
            prior = self.placed.get(key)
            if prior is not None:
                if prior[0] != fields:
                    raise Conflict("client_order_id already used with different fields")
                return prior[1], False
            self._commit(self._plan_place(account, client_order_id, *fields))
            return self.placed[key][1], True

    def cancel(self, order_id: str) -> dict:
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                raise NotFound(order_id)
            if order.status != "open":
                raise Conflict(f"order is {order.status}")
            event = self._event("cancelled", time.time(), order_id=order_id,
                                remaining=order.remaining)
            self._commit([event])
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
            return [dict(e) for e in self.events[after:after + limit]]

    # -- planning: read-only, produces the events a command will cause

    def _plan_place(self, account, client_order_id, symbol, side, price, qty) -> list[dict]:
        ts, order_id = time.time(), uuid.uuid4().hex
        events = [self._event("accepted", ts, order_id=order_id, client_order_id=client_order_id,
                              account=account, symbol=symbol, side=side, price=price, qty=qty)]
        book, opposite, remaining = self.books.get(symbol), ("sell" if side == "buy" else "buy"), qty
        for level in (book.best_first(opposite) if book else []):
            if remaining == 0 or (side == "buy" and level > price) or (side == "sell" and level < price):
                break
            for maker in book.levels[opposite][level]:
                fill = min(remaining, maker.remaining)
                remaining -= fill
                buyer, seller = (account, maker.account) if side == "buy" else (maker.account, account)
                events.append(self._event(
                    "fill", ts, offset=len(events), symbol=symbol, price=level, qty=fill,
                    maker_order_id=maker.order_id, taker_order_id=order_id,
                    buyer=buyer, seller=seller))
                if remaining == 0:
                    break
        return events

    def _event(self, type_: str, ts: float, offset: int = 0, **fields) -> dict:
        return {"seq": len(self.events) + 1 + offset, "ts": ts, "type": type_, **fields}

    # -- applying: the only code that mutates state, shared by live path and recovery

    def _commit(self, events: list[dict]) -> None:
        if self.log is not None:
            self.log.append(events)  # write-ahead: nothing changes unless this succeeds
        self._apply_record(events)

    def _apply_record(self, events: list[dict]) -> None:
        for event in events:
            self._apply(event)
        first = events[0]
        if first["type"] == "accepted":
            key = (first["account"], first["client_order_id"])
            fields = (first["symbol"], first["side"], first["price"], first["qty"])
            response = {"order": asdict(self.orders[first["order_id"]]),
                        "fills": [dict(e) for e in events[1:]], "seq": first["seq"]}
            self.placed[key] = (fields, response)

    def _apply(self, e: dict) -> None:
        if e["seq"] != len(self.events) + 1:
            raise RuntimeError(f"event log out of order at seq {e['seq']}")
        kind = e["type"]
        if kind == "accepted":
            order = Order(e["order_id"], e["client_order_id"], e["account"], e["symbol"],
                          e["side"], e["price"], e["qty"], e["qty"], seq=e["seq"])
            self.orders[order.order_id] = order
            self.books[order.symbol].add(order)
        elif kind == "fill":
            maker, taker = self.orders[e["maker_order_id"]], self.orders[e["taker_order_id"]]
            for order in (maker, taker):
                order.remaining -= e["qty"]
                if order.remaining == 0:
                    order.status = "filled"
                    self.books[order.symbol].remove(order)
            notional = e["price"] * e["qty"]
            buyer, seller = self.positions[e["buyer"]][e["symbol"]], self.positions[e["seller"]][e["symbol"]]
            buyer["position"] += e["qty"]
            buyer["cash"] -= notional
            seller["position"] -= e["qty"]
            seller["cash"] += notional
        elif kind == "cancelled":
            order = self.orders[e["order_id"]]
            order.status = "cancelled"
            self.books[order.symbol].remove(order)
        else:
            raise RuntimeError(f"unknown event type {kind!r}")
        self.events.append(e)
