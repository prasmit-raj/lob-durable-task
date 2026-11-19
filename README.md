# lob-durable-task

A long-horizon task pack: take an in-memory limit order book service and make it crash-safe. A process killed at any point has to come back with the same books, orders, positions and event history, with idempotent order placement on top.

```
task/prompt.md      the prompt the model gets
environment/        the starting repo; becomes /app in the image
verifier/           test.sh, hidden tests, integrity checks
solution/           reference solution (solve.sh copies it into /app)
```

## The service

FastAPI app, started with `python -m lob`. Prices are integer ticks, quantities integer lots.

| Endpoint | What it does |
|---|---|
| `POST /orders` | Place a limit order; matches immediately, rest goes on the book |
| `DELETE /orders/{id}` | Cancel what's left (409 if already filled or cancelled) |
| `GET /orders/{id}` | Current order state |
| `GET /book/{symbol}` | Aggregated depth, best price first |
| `GET /accounts/{account}` | Position and cash per symbol |
| `GET /events?after=N&limit=M` | Event feed: `accepted`, `fill`, `cancelled`, gapless `seq` |
| `GET /health` | Readiness |

```bash
cd environment
pip install -r requirements.txt -r requirements-dev.txt
LOB_PORT=8000 python -m lob &
curl -s localhost:8000/orders -H 'content-type: application/json' \
  -d '{"account":"a","client_order_id":"1","symbol":"AAA","side":"sell","price":100,"qty":5}'
curl -s localhost:8000/book/AAA
```

## Running the pack

Build the image (amd64, matching the grader):

```bash
docker build --platform=linux/amd64 -t lob-task environment/
```

Starting code against the verifier (should fail 10 of 12):

```bash
docker run --rm --network none -v "$PWD/verifier:/verifier:ro" lob-task bash /verifier/test.sh
```

Reference solution (should pass 12 of 12):

```bash
docker run --rm --network none \
  -v "$PWD/verifier:/verifier:ro" -v "$PWD/solution:/solution:ro" \
  lob-task bash -c "bash /solution/solve.sh && bash /verifier/test.sh"
```

A full run takes about 40 seconds. Most of that is the throughput test and the kill loops.

Without Docker, use a plain `python3.12 -m venv` (not `uv venv`, whose `_virtualenv.pth` trips the integrity check):

```bash
python3.12 -m venv /tmp/v && /tmp/v/bin/pip install -r environment/requirements.txt -r environment/requirements-dev.txt
cp -r environment /tmp/app && LOB_APP_DIR=/tmp/app bash solution/solve.sh
PATH=/tmp/v/bin:$PATH SITE_DIR=/tmp/v/lib/python3.12/site-packages \
  VERIFIER_DIR=$PWD/verifier LOB_APP_DIR=/tmp/app bash verifier/test.sh
```

## What the verifier checks

| Test | Prompt sentence |
|---|---|
| `test_basics::test_matching_api_unchanged` | keep matching rules, endpoints, shapes |
| `test_basics::test_state_survives_restart` | 2xx survives a restart |
| `test_basics::test_ack_means_durable_immediately` | 2xx survives SIGKILL, kill lands right after the ack |
| `test_crash::test_kill_under_concurrent_load` | in-flight is all-or-nothing; state equals replaying `/events` |
| `test_crash::test_history_identical_after_crash` | old events keep `seq`, `ts`, ids |
| `test_crash::test_kill_during_recovery` | kill can land during startup; `/health` means recovered |
| `test_crash::test_seq_continues_after_restart` | next `seq`, no gaps or repeats |
| `test_idempotency::*` (4 tests) | 201 / 200 same body / 409 / concurrent duplicates once |
| `test_throughput::test_throughput_with_long_history` | 200 orders/s with 20k orders of history |

State is never checked against a fixed expected value. `harness.Oracle` is an independent price-time matcher: it replays the `accepted` and `cancelled` events from the service's own feed, predicts every fill, and the service's books, orders and accounts must match. Any valid design passes: a WAL, snapshots plus a log, SQLite, whatever.

`test.sh` runs the tests from a temp dir with its own `pytest.ini`, `python -I`, and plugin autoload off, so a `conftest.py` or `pytest.ini` left in `/app` is never loaded. `integrity.py` runs with `-I -S` before pytest and fails the attempt if pytest/pluggy files differ from the pinned release or if a `.pth` file would run code at startup. `check_report.py` then requires exactly 12 testcases with none failed, errored or skipped.

## Before shipping

- Pin the base image by digest. The Dockerfile uses a tag because the digest has to come from your registry:
  ```bash
  docker buildx imagetools inspect python:3.12.8-slim-bookworm --format '{{json .Manifest.Digest}}'
  ```
  then change `FROM` to `python:3.12.8-slim-bookworm@sha256:...`.
- If you bump pytest or pluggy, regenerate `verifier/pytest-sha256.txt` from a clean install:
  ```bash
  cd <site-packages> && find _pytest pytest pluggy -name '*.py' | sort | xargs sha256sum
  ```
- Run the reference solution three times in a row with `--network none` to confirm it isn't flaky.
