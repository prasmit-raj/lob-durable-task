import pytest

from lob.engine import Conflict, Engine


@pytest.fixture
def eng():
    return Engine()


def test_resting_order_shows_in_book(eng):
    eng.place("a", "1", "ABC", "buy", 100, 5)
    assert eng.depth("ABC") == {"bids": [[100, 5]], "asks": []}


def test_price_time_priority(eng):
    first = eng.place("a", "1", "ABC", "sell", 101, 3)["order"]["order_id"]
    second = eng.place("b", "2", "ABC", "sell", 101, 3)["order"]["order_id"]
    eng.place("c", "3", "ABC", "sell", 100, 1)
    res = eng.place("d", "4", "ABC", "buy", 101, 5)
    assert [(f["price"], f["qty"]) for f in res["fills"]] == [(100, 1), (101, 3), (101, 1)]
    assert res["fills"][1]["maker_order_id"] == first
    assert eng.get_order(second)["remaining"] == 2


def test_positions_follow_fills(eng):
    eng.place("a", "1", "ABC", "sell", 50, 4)
    eng.place("b", "2", "ABC", "buy", 55, 4)
    assert eng.account("a")["positions"]["ABC"] == {"position": -4, "cash": 200}
    assert eng.account("b")["positions"]["ABC"] == {"position": 4, "cash": -200}


def test_cancel_removes_from_book(eng):
    oid = eng.place("a", "1", "ABC", "buy", 99, 2)["order"]["order_id"]
    eng.cancel(oid)
    assert eng.depth("ABC")["bids"] == []
    with pytest.raises(Conflict):
        eng.cancel(oid)


def test_seq_is_gapless(eng):
    eng.place("a", "1", "ABC", "sell", 10, 1)
    eng.place("b", "2", "ABC", "buy", 10, 1)
    assert [e["seq"] for e in eng.events_after(0, 100)] == [1, 2, 3]
