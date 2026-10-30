#!/bin/sh
# Install-and-run portability check for the demo manifest (A11).
#
# Env: PYTHON = interpreter to build and install with, REPO = checkout path, WORK = a fresh directory.
# Exit codes: 0 = passed, 1 = failed, 3 = NOT RUN (pip or setuptools unavailable, e.g. a uv-managed
# interpreter without pip). Installs use --no-index, so no network is needed.
set -eu

: "${PYTHON:?set PYTHON to the interpreter to use}"
: "${REPO:?set REPO to the checkout path}"
: "${WORK:?set WORK to a fresh directory}"

not_run() {
    echo "NOT RUN: $1"
    exit 3
}

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

"$PYTHON" -m pip --version >/dev/null 2>&1 || not_run "$PYTHON has no pip"
"$PYTHON" -c 'import setuptools' >/dev/null 2>&1 || not_run "$PYTHON has no setuptools"

SPEC="$REPO/examples/experiments/local-command.json"
mkdir -p "$WORK/dist" "$WORK/elsewhere" "$WORK/bin-only"

# Run the console script from a foreign directory with a PATH that holds no python at all.
# $1 = venv directory, $2 = label.
run_from_elsewhere() {
    venv="$1"
    label="$2"
    rm -f "$WORK/bin-only/harness-workbench"
    cp "$venv/bin/harness-workbench" "$WORK/bin-only/harness-workbench"
    rm -rf "$WORK/root-$label"
    (
        PATH="$WORK/bin-only:/usr/bin:/bin"
        export PATH
        cd "$WORK/elsewhere"
        if command -v python >/dev/null 2>&1; then
            fail "$label: a python executable is on PATH ($(command -v python))"
        fi
        harness-workbench experiment --provider command --spec "$SPEC" \
            --root "$WORK/root-$label" --json >"$WORK/out-$label.json" 2>"$WORK/err-$label.txt"
    ) || fail "$label: harness-workbench exited non-zero: $(cat "$WORK/err-$label.txt" 2>/dev/null)"
    "$venv/bin/python" - "$WORK/out-$label.json" "$label" <<'PY' || fail "$label: unexpected decision"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    status = json.load(handle)["decision"]["status"]
if status != "exploratory_only":
    sys.exit(f"{sys.argv[2]}: decision.status is {status!r}, expected 'exploratory_only'")
PY
    echo "ok: $label"
}

# 1. Wheel install.
"$PYTHON" -m pip wheel "$REPO" --no-deps -w "$WORK/dist" >"$WORK/wheel.log" 2>&1 \
    || not_run "could not build a wheel offline (see $WORK/wheel.log): $(tail -n 1 "$WORK/wheel.log")"
"$PYTHON" -m venv "$WORK/venv" >"$WORK/venv.log" 2>&1 || not_run "$PYTHON cannot create a venv with pip"
"$WORK/venv/bin/python" -m pip install --no-index "$WORK"/dist/harness_workbench-*.whl >"$WORK/install.log" 2>&1 \
    || fail "wheel install failed: $(tail -n 3 "$WORK/install.log")"
run_from_elsewhere "$WORK/venv" wheel

# 2. Editable install. The venv sees the interpreter's setuptools so the build needs no index.
"$PYTHON" -m venv --system-site-packages "$WORK/venv-editable" >"$WORK/venv2.log" 2>&1 \
    || not_run "$PYTHON cannot create a second venv"
"$WORK/venv-editable/bin/python" -m pip install --no-index --no-build-isolation -e "$REPO" \
    >"$WORK/install-editable.log" 2>&1 || fail "editable install failed: $(tail -n 3 "$WORK/install-editable.log")"
location="$("$WORK/venv-editable/bin/python" -c 'import harness_workbench, pathlib; print(pathlib.Path(harness_workbench.__file__).resolve().parent.parent)')"
[ "$location" = "$(cd "$REPO" && pwd -P)" ] || fail "editable install imports harness_workbench from $location, not $REPO"
run_from_elsewhere "$WORK/venv-editable" editable

echo "PASS: wheel and editable installs ran the demo manifest from a foreign cwd with no python on PATH"
