#!/bin/bash
# Grades an attempt. The service under test is the code in $LOB_APP_DIR (default /app);
# the test runner itself never loads anything from there.
set -euo pipefail

VERIFIER_DIR="${VERIFIER_DIR:-/verifier}"
export LOB_APP_DIR="${LOB_APP_DIR:-/app}"
EXPECTED_TESTS=12
PY="$(command -v python3)"

"$PY" -I -S "$VERIFIER_DIR/integrity.py" "$VERIFIER_DIR/pytest-sha256.txt" ${SITE_DIR:+"$SITE_DIR"}

WORK="$(mktemp -d)"
cp -r "$VERIFIER_DIR/tests" "$WORK/tests"
cp "$VERIFIER_DIR/pytest.ini" "$WORK/pytest.ini"
cd "$WORK"

# Plugins only load when named; config and rootdir are ours, not the repo's.
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PY" -I -m pytest tests -q \
  -c "$WORK/pytest.ini" --rootdir="$WORK" \
  --junitxml="$WORK/report.xml" || true

"$PY" -I -S "$VERIFIER_DIR/check_report.py" "$WORK/report.xml" "$EXPECTED_TESTS"
