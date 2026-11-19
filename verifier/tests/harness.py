"""Shared helpers for the hidden tests: run the service, talk HTTP, replay the feed."""
from __future__ import annotations

import http.client
import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque

APP_DIR = os.environ.get("LOB_APP_DIR", "/app")
SYMBOLS = ["AAA", "BBB"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """One keep-alive connection; use one per thread."""

    def __init__(self, port: int, timeout: float = 10.0) -> None:
        self.port, self.timeout = port, timeout
        self.conn: http.client.HTTPConnection | None = None

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if self.conn is None:
            self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        try:
            self.conn.request(method, path, body=payload, headers=headers)
            resp = self.conn.getresponse()
            data = resp.read()
        except Exception:
            self.close()
            raise
        return resp.status, (json.loads(data) if data else {})

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class Server:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = str(data_dir)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    def start(self, wait: bool = True, timeout: float = 60.0) -> None:
        env = dict(os.environ, LOB_DATA_DIR=self.data_dir, LOB_PORT=str(self.port))
        self.proc = subprocess.Popen([sys.executable, "-m", "lob"], cwd=APP_DIR, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            self.wait_healthy(timeout)

    def wait_healthy(self, timeout: float) -> float:
        start = time.monotonic()
        client = Client(self.port, timeout=2)
        while time.monotonic() - start < timeout:
            if self.proc.poll() is not None:
                raise AssertionError(f"service exited with {self.proc.returncode} during startup")
            try:
                if client.request("GET", "/health")[0] == 200:
                    return time.monotonic() - start
            except OSError:
                pass
            time.sleep(0.02)
        raise AssertionError("service did not become healthy")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
        if self.proc:
            self.proc.wait()

    def restart(self) -> None:
        self.kill()
        self.start()

    def client(self) -> Client:
        return Client(self.port)


def place(c: Client, account: str, coid: str, symbol: str, side: str, price: int, qty: int):
    return c.request("POST", "/orders", {"account": account, "client_order_id": coid,
                                         "symbol": symbol, "side": side, "price": price, "qty": qty})


def all_events(c: Client) -> list[dict]:
    events, after = [], 0
    while True:
        status, body = c.request("GET", f"/events?after={after}&limit=1000")
        assert status == 200
        if not body["events"]:
            return events
        events.extend(body["events"])
        after = body["events"][-1]["seq"]


# ---------------------------------------------------------------- oracle

class Oracle:
    """Independent price-time matcher. Fed the accepted/cancelled events in seq
    order, it predicts every fill and the resulting books, orders and positions."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.levels = {s: {"buy": defaultdict(deque), "sell": defaultdict(deque)} for s in SYMBOLS}
        self.positions = defaultdict(lambda: defaultdict(lambda: {"position": 0, "cash": 0}))

    def accept(self, e: dict) -> list[dict]:
        o = {k: e[k] for k in ("order_id", "client_order_id", "account", "symbol", "side", "price", "qty")}
        o.update(remaining=e["qty"], status="open", seq=e["seq"])
        self.orders[o["order_id"]] = o
        opp = "sell" if o["side"] == "buy" else "buy"
        book = self.levels[o["symbol"]][opp]
        fills = []
        while o["remaining"]:
            live = [p for p, q in book.items() if q]
            if not live:
                break
            price = min(live) if opp == "sell" else max(live)
            if (o["side"] == "buy" and price > o["price"]) or (o["side"] == "sell" and price < o["price"]):
                break
            maker = book[price][0]
            qty = min(o["remaining"], maker["remaining"])
            maker["remaining"] -= qty
            o["remaining"] -= qty
            buyer, seller = (o, maker) if o["side"] == "buy" else (maker, o)
            fills.append({"type": "fill", "symbol": o["symbol"], "price": price, "qty": qty,
                          "maker_order_id": maker["order_id"], "taker_order_id": o["order_id"],
                          "buyer": buyer["account"], "seller": seller["account"]})
            self.positions[buyer["account"]][o["symbol"]]["position"] += qty
            self.positions[buyer["account"]][o["symbol"]]["cash"] -= price * qty
            self.positions[seller["account"]][o["symbol"]]["position"] -= qty
            self.positions[seller["account"]][o["symbol"]]["cash"] += price * qty
            if maker["remaining"] == 0:
                maker["status"] = "filled"
                book[price].popleft()
        if o["remaining"]:
            self.levels[o["symbol"]][o["side"]][o["price"]].append(o)
        else:
            o["status"] = "filled"
        return fills

    def cancel(self, e: dict) -> None:
        o = self.orders.get(e["order_id"])
        assert o is not None and o["status"] == "open", f"cancel of non-open order: {e}"
        assert e["remaining"] == o["remaining"], f"cancel remaining mismatch: {e}"
        self.levels[o["symbol"]][o["side"]][o["price"]].remove(o)
        o["status"] = "cancelled"

    def depth(self, symbol: str) -> dict:
        def side(name: str, reverse: bool) -> list:
            lv = self.levels[symbol][name]
            return [[p, sum(o["remaining"] for o in lv[p])] for p in sorted(lv, reverse=reverse) if lv[p]]
        return {"bids": side("buy", True), "asks": side("sell", False)}


def strip(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in ("seq", "ts")}


def replay(events: list[dict]) -> Oracle:
    """Check the feed is gapless and internally consistent; return the oracle state."""
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "feed seq has gaps or duplicates"
    oracle, i = Oracle(), 0
    while i < len(events):
        e = events[i]
        if e["type"] == "accepted":
            expected = oracle.accept(e)
            got = [strip(x) for x in events[i + 1:i + 1 + len(expected)]]
            assert got == expected, f"fills after seq {e['seq']} do not match price-time matching"
            i += 1 + len(expected)
            if i < len(events):
                assert events[i]["type"] != "fill", f"unexpected fill at seq {events[i]['seq']}"
        elif e["type"] == "cancelled":
            oracle.cancel(e)
            i += 1
        else:
            raise AssertionError(f"unexpected event at seq {e['seq']}: {e}")
    return oracle


def assert_state_matches_feed(srv: Server) -> list[dict]:
    """Service state must equal what the surviving feed implies."""
    c = srv.client()
    events = all_events(c)
    oracle = replay(events)
    for sym in SYMBOLS:
        assert c.request("GET", f"/book/{sym}") == (200, oracle.depth(sym)), f"book {sym} differs from feed"
    for oid, o in oracle.orders.items():
        status, body = c.request("GET", f"/orders/{oid}")
        assert status == 200 and body == o, f"order {oid} differs from feed"
    for acct, held in oracle.positions.items():
        status, body = c.request("GET", f"/accounts/{acct}")
        assert status == 200 and body["positions"] == {s: dict(p) for s, p in held.items()}, acct
    c.close()
    return events


# ---------------------------------------------------------------- load

class Load:
    """Concurrent clients placing and cancelling; records every 2xx response."""

    def __init__(self, srv: Server, clients: int = 16, seed: int = 0, prefix: str = "L") -> None:
        self.srv, self.clients, self.seed, self.prefix = srv, clients, seed, prefix
        self.stop = threading.Event()
        self.acked_orders: list[dict] = []
        self.acked_cancels: list[dict] = []
        self.mu = threading.Lock()
        self.threads: list[threading.Thread] = []

    def _run(self, n: int) -> None:
        rng = random.Random(self.seed * 1000 + n)
        c, mine, k = self.srv.client(), [], 0
        while not self.stop.is_set():
            try:
                if mine and rng.random() < 0.2:
                    oid = mine.pop(rng.randrange(len(mine)))
                    status, body = c.request("DELETE", f"/orders/{oid}")
                    if status == 200:
                        with self.mu:
                            self.acked_cancels.append(body)
                else:
                    k += 1
                    status, body = place(c, f"acct{n % 6}", f"{self.prefix}{self.seed}-{n}-{k}",
                                         rng.choice(SYMBOLS), rng.choice(["buy", "sell"]),
                                         100 + rng.randint(-4, 4), rng.randint(1, 9))
                    if status in (200, 201):
                        with self.mu:
                            self.acked_orders.append(body)
                        if body["order"]["status"] == "open":
                            mine.append(body["order"]["order_id"])
            except OSError:
                break  # server went away
            except http.client.HTTPException:
                break
        c.close()

    def start(self) -> "Load":
        self.threads = [threading.Thread(target=self._run, args=(n,), daemon=True)
                        for n in range(self.clients)]
        for t in self.threads:
            t.start()
        return self

    def join(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=30)


def assert_acked_survived(srv: Server, load: Load, events: list[dict]) -> None:
    by_seq = {e["seq"]: e for e in events}
    c = srv.client()
    for body in load.acked_orders:
        accepted = by_seq.get(body["seq"])
        assert accepted and accepted["type"] == "accepted" and accepted["order_id"] == body["order"]["order_id"], \
            f"acknowledged order {body['order']['order_id']} lost"
        for f in body["fills"]:
            assert by_seq.get(f["seq"]) == f, f"acknowledged fill seq {f['seq']} lost or changed"
    for body in load.acked_cancels:
        e = by_seq.get(body["seq"])
        assert e and e["type"] == "cancelled" and e["order_id"] == body["order"]["order_id"], "acknowledged cancel lost"
        assert c.request("GET", f"/orders/{body['order']['order_id']}")[1]["status"] == "cancelled"
    c.close()
