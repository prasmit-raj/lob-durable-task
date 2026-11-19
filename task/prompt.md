The service in /app (`lob`) is a limit order book: orders match by price-time priority, and everything lives in memory, so a restart wipes it. Make it durable. It's still started with `python -m lob` (port from `LOB_PORT`) and must keep all its state under the directory named by `LOB_DATA_DIR`, defaulting to `/app/data`.

What has to hold:

- Anything that got a 2xx response is still there after the process is killed with SIGKILL and started again, no matter when the kill lands, including while the service is starting up. `/health` returns 200 only once the service is ready to serve the recovered state.
- A request that was in flight when the process died is applied fully or not at all. After a restart, every book, order and account must be exactly what you get by applying the events in `/events` in `seq` order under the existing matching rules.
- Events that existed before a restart come back with the same `seq` and identical fields, `ts` and ids included. New events continue from the next `seq`, with no gaps or repeats.
- `POST /orders` becomes idempotent per account on `client_order_id`. The first placement returns 201 as today. Repeating it with the same fields returns 200 and exactly the body the first call returned, even if the order has since traded, been cancelled, or the service restarted. Reusing the key with any field changed returns 409. Concurrent duplicates must place the order once.
- Durability can't make it slow: with 20,000 orders already in the history, 16 concurrent clients must still get at least 200 orders per second accepted.

Keep the matching rules, endpoints and response shapes as they are. You can't install anything new, so build on what's in the image.
