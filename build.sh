#!/usr/bin/env bash
# Render build: Python deps + Playwright Chromium browser binary.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -f backend/requirements.txt ]; then
  REQ="backend/requirements.txt"
elif [ -f requirements.txt ]; then
  REQ="requirements.txt"
else
  echo "ERROR: requirements.txt not found" >&2
  exit 1
fi

python -m pip install --upgrade pip
python -m pip install -r "$REQ"

# Persist browsers in the project tree (Render-friendly).
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.playwright}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

echo "Installing Playwright Chromium into $PLAYWRIGHT_BROWSERS_PATH ..."
python -m playwright install chromium

# Optional system libs (ignore failure on restricted builders).
python -m playwright install-deps chromium 2>/dev/null || true

echo "Verifying Chromium launch..."
python - <<'PY'
import os
print("PLAYWRIGHT_BROWSERS_PATH=", os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    b.close()
print("Playwright Chromium verified OK")
PY
