#!/bin/sh
set -eu

workspace=${1:-./fruit-and-veg-workspace}
port=${ORCHESTRA_PORT:-8080}
scheduler_pid=
python=${PYTHON:-python}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ORCHESTRA_SOURCE=$script_dir
export ORCHESTRA_SOURCE

run_orc() {
    "$python" -c 'import os, sys; sys.path.insert(0, os.environ["ORCHESTRA_SOURCE"]); from orchestration.cli import main; main()' "$@"
}

cleanup() {
    trap - EXIT INT TERM
    if [ -n "$scheduler_pid" ] && kill -0 "$scheduler_pid" 2>/dev/null; then
        kill "$scheduler_pid" 2>/dev/null || true
        wait "$scheduler_pid" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

run_orc --base-dir "$workspace" scheduler run &
scheduler_pid=$!

printf 'Opening Orchestra at http://localhost:%s\n' "$port"
run_orc --base-dir "$workspace" worksm start --port "$port"