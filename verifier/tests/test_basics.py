"""Existing behaviour still works, and a plain restart keeps state."""
import pytest

from harness import Server, all_events, assert_state_matches_feed, place


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path / "data")
    s.start()
    yield s
    s.kill()


def test_matching_api_unchanged(srv):
    c = srv.client()
    status, rest = place(c, "a", "r1", "AAA", "sell", 101, 3)
    assert status == 201 and rest["order"]["status"] == "open" and rest["fills"] == []
    place(c, "b", "r2", "AAA", "sell", 100, 1)
    status, take = place(c, "c", "t1", "AAA", "buy", 101, 5)
    assert status == 201
    assert [(f["price"], f["qty"]) for f in take["fills"]] == [(100, 1), (101, 3)]
    assert take["order"]["remaining"] == 1 and take["order"]["status"] == "open"
    assert c.request("GET", "/book/AAA") == (200, {"bids": [[101, 1]], "asks": []})
    assert c.request("GET", "/accounts/c")[1]["positions"]["AAA"] == {"position": 4, "cash": -403}
    status, body = c.request("DELETE", f"/orders/{take['order']['order_id']}")
    assert status == 200 and body["order"]["status"] == "cancelled"
    assert c.request("DELETE", f"/orders/{take['order']['order_id']}")[0] == 409
    assert c.request("GET", "/orders/nope")[0] == 404
    assert_state_matches_feed(srv)


def test_state_survives_restart(srv):
    c = srv.client()
    for i in range(30):
        place(c, f"a{i % 3}", f"x{i}", "AAA" if i % 2 else "BBB",
              "buy" if i % 3 else "sell", 98 + i % 5, 1 + i % 4)
    before = assert_state_matches_feed(srv)
    srv.restart()
    after = assert_state_matches_feed(srv)
    assert after == before


def test_ack_means_durable_immediately(srv):
    # Kill right after the last 2xx with no pause, so buffered or deferred writes are lost.
    c = srv.client()
    acked = [place(c, "a", f"d{i}", "AAA", "buy" if i % 2 else "sell", 100, 1)[1] for i in range(200)]
    srv.kill()
    srv.start()
    events = all_events(srv.client())
    assert len(events) >= acked[-1]["seq"]
    assert {e["order_id"] for e in events if e["type"] == "accepted"} == {a["order"]["order_id"] for a in acked}
