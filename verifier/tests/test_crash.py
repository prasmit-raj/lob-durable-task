"""SIGKILL at arbitrary points: under load, and during startup recovery."""
import random
import time

from harness import Load, Server, all_events, assert_acked_survived, assert_state_matches_feed, place


def test_kill_under_concurrent_load(tmp_path):
    srv = Server(tmp_path / "data")
    srv.start()
    rng = random.Random(7)
    previous: list[dict] = []
    try:
        for round_ in range(5):
            load = Load(srv, clients=16, seed=round_).start()
            time.sleep(rng.uniform(0.3, 1.5))
            srv.kill()
            load.join()
            srv.start()
            events = assert_state_matches_feed(srv)
            assert events[:len(previous)] == previous, "history changed across a restart"
            assert_acked_survived(srv, load, events)
            assert len(events) > len(previous), "no progress was made under load"
            previous = events
    finally:
        srv.kill()


def test_history_identical_after_crash(tmp_path):
    # ts, order_id and every other field of past events must survive recovery unchanged.
    srv = Server(tmp_path / "data")
    srv.start()
    try:
        load = Load(srv, clients=8, seed=42).start()
        time.sleep(0.5)
        load.join()
        quiescent = all_events(srv.client())
        load = Load(srv, clients=8, seed=43, prefix="M").start()
        time.sleep(0.4)
        srv.kill()
        load.join()
        srv.start()
        assert all_events(srv.client())[:len(quiescent)] == quiescent
    finally:
        srv.kill()


def test_kill_during_recovery(tmp_path):
    srv = Server(tmp_path / "data")
    srv.start()
    rng = random.Random(3)
    try:
        load = Load(srv, clients=16, seed=9).start()
        time.sleep(1.5)
        load.join()
        before = assert_state_matches_feed(srv)
        for _ in range(10):
            srv.kill()
            srv.start(wait=False)
            time.sleep(rng.uniform(0.0, 0.6))
        srv.kill()
        srv.start()
        assert assert_state_matches_feed(srv) == before
        status, body = place(srv.client(), "z", "after-recovery", "AAA", "buy", 1, 1)
        assert status == 201 and body["seq"] == len(before) + 1
    finally:
        srv.kill()


def test_seq_continues_after_restart(tmp_path):
    srv = Server(tmp_path / "data")
    srv.start()
    try:
        c = srv.client()
        place(c, "a", "s1", "AAA", "sell", 100, 2)
        last = place(c, "b", "s2", "AAA", "buy", 100, 1)[1]["fills"][-1]["seq"]
        srv.kill()
        srv.start()
        status, body = place(srv.client(), "a", "s3", "BBB", "buy", 90, 1)
        assert status == 201 and body["seq"] == last + 1
        assert_state_matches_feed(srv)
    finally:
        srv.kill()
