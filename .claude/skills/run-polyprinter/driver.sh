#!/usr/bin/env bash
# Curl-based driver/smoke test for PolyPrinter. Run from the repo root
# (paths below are relative to <unit>/, i.e. the polyprinter/ repo root —
# see SKILL.md). Brings the real stack up with `docker compose`, drives it
# exactly the way a human/agent would (seed the Phase 0 fixture, run Scout
# against live Polymarket data, hit every dashboard route), and asserts on
# what comes back. Exits non-zero on the first failed assertion.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."   # -> repo root from .claude/skills/run-polyprinter/

DASH=http://127.0.0.1:8765
FAIL=0

# Never let a driver run destroy a real, populated dashboard database — this
# actually happened TWICE (2026-08-07, then again 2026-08-08): a verification
# run wiped a live trader DB, and the next thing anyone saw was a dashboard
# reading all zeros. The first fix attempt moved the live db aside to
# data/polyprinter.db.driver-backup — but the "reset db" step further down
# (`rm -f data/polyprinter.db*`) globs on the same prefix and matched the
# backup files too, deleting them before the EXIT trap could restore them.
# Fixed for real this time by parking the backup in its own subdirectory,
# outside every glob pattern this script uses on data/*.
DB_FILES=(data/polyprinter.db data/polyprinter.db-wal data/polyprinter.db-shm)
BACKUP_DIR="data/.driver-backup"

restore_live_db() {
  rm -f "${DB_FILES[@]}"
  if [ -d "$BACKUP_DIR" ]; then
    for f in "${DB_FILES[@]}"; do
      local_name="$(basename "$f")"
      [ -f "$BACKUP_DIR/$local_name" ] && mv "$BACKUP_DIR/$local_name" "$f"
    done
    rmdir "$BACKUP_DIR" 2>/dev/null || true
  fi
}
trap restore_live_db EXIT

echo "== set aside live db (if present), driver gets a clean slate =="
if [ -f "${DB_FILES[0]}" ]; then
  mkdir -p "$BACKUP_DIR"
  for f in "${DB_FILES[@]}"; do
    [ -f "$f" ] && mv "$f" "$BACKUP_DIR/$(basename "$f")"
  done
fi

check_status() {
  local path="$1" want="$2"
  local got
  got=$(curl -s -o /dev/null -w "%{http_code}" "$DASH$path")
  if [ "$got" != "$want" ]; then
    echo "FAIL  GET $path -> $got (wanted $want)"
    FAIL=1
  else
    echo "ok    GET $path -> $got"
  fi
}

check_contains() {
  # Retries a few times before failing — seen twice (2026-08-08) as a
  # one-off transient miss immediately after a scout run, on static
  # template text that can't legitimately depend on the data just
  # written, then passing clean on the very next attempt. Root cause
  # not pinned down (Flask's dev server here isn't threaded and isn't
  # in reload mode, so the two obvious explanations don't fit); a short
  # retry is cheap insurance against whatever it is rather than a false
  # DRIVER FAILED on a real, working page.
  local path="$1" needle="$2" attempt
  for attempt in 1 2 3; do
    if curl -s "$DASH$path" | grep -qF "$needle"; then
      echo "ok    GET $path contains '$needle'"
      return
    fi
    [ "$attempt" -lt 3 ] && sleep 1
  done
  echo "FAIL  GET $path does NOT contain '$needle' (after $attempt attempts)"
  FAIL=1
}

echo "== build =="
docker compose build

echo "== reset db (fresh run) =="
rm -f data/polyprinter.db* data/*.log

echo "== up: dashboard =="
docker compose up -d dashboard
sleep 2

echo "== loopback-only binding check =="
BOUND=$(docker compose port dashboard 8765)
if [ "$BOUND" != "127.0.0.1:8765" ]; then
  echo "FAIL  dashboard port published as '$BOUND', expected '127.0.0.1:8765'"
  FAIL=1
else
  echo "ok    dashboard published on $BOUND only"
fi

echo "== Phase 0 exit criterion: seed a demo decision, confirm it renders =="
docker compose exec dashboard python scripts/seed_demo_decision.py
check_contains /decisions "Phase 0 fixture row"

echo "== route smoke test =="
check_status / 200
check_status /traders 200
check_status /decisions 200
check_status /calibration 200
check_status /us-vs-them 200
check_status /traders/0xdoes-not-exist 404

echo "== Phase 1: run Scout against LIVE Polymarket data (small pool, fast) =="
docker compose run --rm scout python -m polyprinter.scout.run --leaderboard-limit 5

echo "== confirm real trader data rendered =="
check_contains /traders "ROI (shrunk)"
TRADER_ROWS=$(curl -s "$DASH/traders" | grep -c '<a href="/traders/0x' || true)
if [ "$TRADER_ROWS" -lt 1 ]; then
  echo "FAIL  /traders rendered 0 trader rows after a Scout run"
  FAIL=1
else
  echo "ok    /traders rendered $TRADER_ROWS trader row(s)"
fi

echo "== test suite =="
docker run --rm -v "$PWD/tests:/app/tests" polyprinter-scout \
  sh -c "pip install --no-cache-dir pytest -q >/dev/null 2>&1 && python -m pytest tests -q"

echo "== teardown =="
docker compose down

if [ "$FAIL" -ne 0 ]; then
  echo "DRIVER FAILED"
  exit 1
fi
echo "DRIVER PASSED"
