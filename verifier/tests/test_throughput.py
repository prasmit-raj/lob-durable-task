"""Durability must not cost throughput as history grows."""
import threading
import time

from harness import Server, place

CLIENTS = 16


def blast(srv, total, tag):
    per = total // CLIENTS

    def run(n):
        c = srv.client()
        for k in range(per):
            # Alternate sides at one price so orders trade and the book stays small.
            status, _ = place(c, f"t{n % 4}", f"{tag}-{n}-{k}", "AAA",
                              "buy" if (n + k) % 2 else "sell", 100, 1)
            assert status == 201
        c.close()

    threads = [threading.Thread(target=run, args=(n,)) for n in range(CLIENTS)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return per * CLIENTS / (time.monotonic() - start)


def test_throughput_with_long_history(tmp_path):
    srv = Server(tmp_path / "data")
    srv.start()
    try:
        blast(srv, 20_000, "warm")
        rate = blast(srv, 3_200, "measure")
        assert rate >= 200, f"{rate:.0f} orders/s with 20k orders of history"
    finally:
        srv.kill()
