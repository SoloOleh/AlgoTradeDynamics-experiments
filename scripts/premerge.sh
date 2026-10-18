#!/usr/bin/env bash
# One-command pre-merge gate for the exchange-adapter step (spec §6, sad §11 «no CI»).
# Runs: golden-run replays, the reference failure table, the static search, and the existing
# backend and emulator suites. Exits non-zero and names every failing stage.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/BE/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

failed=()
# Every stage gets its own fresh temp dir: tests/conftest.py keeps its SQLite file under
# tempfile.gettempdir(), so a shared $TMPDIR let concurrent runs and stages collide.
RUN_TMP="$(mktemp -d "${TMPDIR:-/tmp}/premerge.XXXXXX")"
trap 'rm -rf "$RUN_TMP"' EXIT

stage() {
  local name="$1" dir="$2" stage_tmp
  shift 2
  echo "==> $name"
  stage_tmp="$(mktemp -d "$RUN_TMP/stage.XXXXXX")"
  if (cd "$dir" && TMPDIR="$stage_tmp" "$@"); then
    echo "    ok: $name"
  else
    echo "    FAILED: $name"
    failed+=("$name")
  fi
}

stage "golden runs" "$ROOT/BE" "$PYTHON" -m pytest -q -m golden tests/golden -p no:cacheprovider
stage "failure table" "$ROOT/BE" "$PYTHON" -m pytest -q tests/golden/test_failure_table.py -p no:cacheprovider
stage "static search" "$ROOT/BE" "$PYTHON" -m pytest -q tests/golden/test_static_search.py -p no:cacheprovider
stage "backend suite" "$ROOT/BE" "$PYTHON" -m pytest -q --ignore=tests/golden -p no:cacheprovider
stage "emulator suite" "$ROOT/EXCHANGE_EMULATOR" "$PYTHON" -m pytest -q -p no:cacheprovider

echo
if [ "${#failed[@]}" -gt 0 ]; then
  echo "pre-merge gate FAILED: ${failed[*]}"
  exit 1
fi
echo "pre-merge gate passed"
