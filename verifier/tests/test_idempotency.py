"""POST /orders is idempotent on (account, client_order_id)."""
import threading

import pytest

from harness import Server, assert_state_matches_feed, place


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path / "data")
    s.start()
    yield s
    s.kill()


def test_retry_returns_original_response(srv):
    c = srv.client()
    status, first = place(c, "a", "k1", "AAA", "buy", 100, 5)
    assert status == 201
    assert place(c, "a", "k1", "AAA", "buy", 100, 5) == (200, first)
    # Later changes to the order do not change what a retry returns.
    place(c, "b", "k1", "AAA", "sell", 100, 2)
    c.request("DELETE", f"/orders/{first['order']['order_id']}")
    assert place(c, "a", "k1", "AAA", "buy", 100, 5) == (200, first)
    srv.restart()
    c = srv.client()
    assert place(c, "a", "k1", "AAA", "buy", 100, 5) == (200, first)
    assert_state_matches_feed(srv)


def test_retry_with_fills_survives_restart(srv):
    c = srv.client()
    place(c, "m", "maker", "BBB", "sell", 50, 3)
    status, first = place(c, "t", "taker", "BBB", "buy", 50, 5)
    assert status == 201 and len(first["fills"]) == 1
    srv.restart()
    assert place(srv.client(), "t", "taker", "BBB", "buy", 50, 5) == (200, first)


def test_mismatched_retry_conflicts(srv):
    c = srv.client()
    assert place(c, "a", "k2", "AAA", "buy", 100, 5)[0] == 201
    assert place(c, "a", "k2", "AAA", "buy", 100, 6)[0] == 409
    assert place(c, "a", "k2", "BBB", "buy", 100, 5)[0] == 409
    srv.restart()
    assert place(srv.client(), "a", "k2", "AAA", "sell", 100, 5)[0] == 409
    # The key is scoped to the account.
    assert place(srv.client(), "other", "k2", "AAA", "buy", 100, 5)[0] == 201
    events = assert_state_matches_feed(srv)
    assert sum(e["type"] == "accepted" for e in events) == 2


def test_concurrent_retries_place_once(srv):
    results = []
    mu = threading.Lock()

    def go():
        r = place(srv.client(), "a", "race", "AAA", "sell", 120, 1)
        with mu:
            results.append(r)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(s for s, _ in results)
    assert statuses == [200] * 11 + [201]
    assert len({b["order"]["order_id"] for _, b in results}) == 1
