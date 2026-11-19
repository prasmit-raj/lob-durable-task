#!/bin/bash
# Applies the reference solution to /app.
set -euo pipefail
cp "$(dirname "$0")"/lob/*.py "${LOB_APP_DIR:-/app}/lob/"
