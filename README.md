# lob-durable-task

Task pack: an in-memory limit order book service that has to survive being killed at any point, and come back with the same books, orders, positions and event history.

- `task/prompt.md` is the prompt
- `environment/` is the starting repo (becomes `/app`)
- `verifier/` has `test.sh` and the hidden tests
- `solution/` is the reference solution

## Run it

```bash
docker build --platform=linux/amd64 -t lob-task environment/

# starting code, fails 10 of 12
docker run --rm --network none -v "$PWD/verifier:/verifier:ro" lob-task bash /verifier/test.sh

# reference solution, passes 12 of 12
docker run --rm --network none \
  -v "$PWD/verifier:/verifier:ro" -v "$PWD/solution:/solution:ro" \
  lob-task bash -c "bash /solution/solve.sh && bash /verifier/test.sh"
```

A run takes about 40 seconds.

No Docker? Use a plain venv (`uv venv` adds a `.pth` file that the integrity check rejects):

```bash
python3.12 -m venv /tmp/v
/tmp/v/bin/pip install -r environment/requirements.txt -r environment/requirements-dev.txt
cp -r environment /tmp/app && LOB_APP_DIR=/tmp/app bash solution/solve.sh
PATH=/tmp/v/bin:$PATH SITE_DIR=/tmp/v/lib/python3.12/site-packages \
  VERIFIER_DIR=$PWD/verifier LOB_APP_DIR=/tmp/app bash verifier/test.sh
```

## How grading works

The tests don't compare against hardcoded answers. `verifier/tests/harness.py` has its own price-time matcher that replays the service's `/events` feed and checks the books, orders and accounts against it. That way any storage design passes as long as the behaviour is right.

`test.sh` runs pytest from a temp dir with its own config, so nothing left in `/app` gets loaded. It also checks pytest's files against pinned hashes, and it requires all 12 tests to run and pass.

## Before shipping

Pin the base image by digest:

```bash
docker buildx imagetools inspect python:3.12.8-slim-bookworm --format '{{json .Manifest.Digest}}'
```

Then change the `FROM` line to `python:3.12.8-slim-bookworm@sha256:...`.

If you change the pytest version, regenerate `verifier/pytest-sha256.txt` by running this from site-packages:

```bash
find _pytest pytest pluggy -name '*.py' | sort | xargs sha256sum
```
