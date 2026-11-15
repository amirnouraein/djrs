#!/usr/bin/env bash
# End-to-end check on the sample project, for the Django installed in $PYTHON:
#   1. extraction is deterministic (two runs, same bytes)
#   2. generation is deterministic (`djrs check`)
#   3. the generated gateway builds
#   4. the project's tests pass on plain Django, and again through the gateway
set -euo pipefail

PY=${PYTHON:-python}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
EXAMPLE=${EXAMPLE:-notes}
SETTINGS=${SETTINGS:-notesite.settings}
OUT=${OUT:-$ROOT/target/e2e/$EXAMPLE}
RUNTIME=$ROOT/crates/djrs-runtime

mkdir -p "$OUT"
cd "$ROOT/examples/$EXAMPLE"

echo "== extract (twice)"
"$PY" -m djrs extract --settings "$SETTINGS" --pythonpath . --out "$OUT/ir.json"
"$PY" -m djrs extract --settings "$SETTINGS" --pythonpath . --out "$OUT/ir-again.json"
cmp "$OUT/ir.json" "$OUT/ir-again.json"

echo "== generate + check"
CONFIG=()
if [ -f djrs.toml ]; then CONFIG=(--config djrs.toml); fi
rm -rf "$OUT/gateway"
"$PY" -m djrs generate "$OUT/ir.json" --out "$OUT/gateway" --runtime "$RUNTIME" "${CONFIG[@]}"
"$PY" -m djrs check "$OUT/ir.json" --out "$OUT/gateway" --runtime "$RUNTIME" "${CONFIG[@]}"

echo "== build gateway"
CARGO_TARGET_DIR="$ROOT/target" cargo build --quiet --manifest-path "$OUT/gateway/Cargo.toml"
BIN="$ROOT/target/debug/$(sed -n 's/^name = "\(.*\)"$/\1/p' "$OUT/gateway/Cargo.toml" | head -1)"

echo "== tests on plain Django"
"$PY" manage.py test "$EXAMPLE" --noinput

echo "== tests through the gateway ($BIN), GET responses compared byte for byte with Django"
DJRS_BINARY="$BIN" DJRS_COMPARE=1 DJRS_COMPARE_STRICT=1 \
  "$PY" manage.py test "$EXAMPLE" --noinput --testrunner djrs.testing.DjrsTestRunner

echo "== tests through the gateway with every view forwarded"
DJRS_BINARY="$BIN" DJRS_FORWARD_ALL=1 DJRS_TEST_COMMIT=0 \
  "$PY" manage.py test "$EXAMPLE" --noinput --testrunner djrs.testing.DjrsTestRunner
