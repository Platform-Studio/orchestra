#!/bin/sh
set -eu

port=${ORCHESTRA_PORT:-8080}
scheduler_pid=
scheduler_output_pid=
server_pid=
server_output_pid=
browser_pid=
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${PYTHON:-}" ]; then
    python=$PYTHON
elif [ -n "${VIRTUAL_ENV:-}" ]; then
    python="$VIRTUAL_ENV/bin/python"
elif [ -x "$script_dir/.venv/bin/python" ]; then
    python="$script_dir/.venv/bin/python"
else
    python=python
fi
ORCHESTRA_SOURCE=$script_dir
export ORCHESTRA_SOURCE

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    orange=$(printf '\033[38;5;208m')
    red=$(printf '\033[31m')
    reset=$(printf '\033[0m')
else
    orange=''
    red=''
    reset=''
fi

usage() {
    printf 'Usage: %s [-p PORT] [workspace]\n' "$0"
}

argument_error() {
    printf '%sERROR: %s%s\n' "$red" "$1" "$reset" >&2
    usage >&2
    exit 2
}

while getopts ':p:h' option; do
    case "$option" in
        p) port=$OPTARG ;;
        h)
            usage
            exit 0
            ;;
        :) argument_error "Option -$OPTARG requires a value." ;;
        \?) argument_error "Unknown option: -$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

if [ "$#" -gt 1 ]; then
    argument_error "Only one workspace path may be provided."
fi
workspace=${1:-./xmas-movies-workspace}

case "$port" in
    ''|*[!0-9]*) argument_error "Port must be an integer from 1 to 65535." ;;
esac
if [ "${#port}" -gt 5 ] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    argument_error "Port must be an integer from 1 to 65535."
fi

output_dir=$(mktemp -d "${TMPDIR:-/tmp}/orchestra-run.XXXXXX")
scheduler_fifo="$output_dir/scheduler"
server_fifo="$output_dir/server"
mkfifo "$scheduler_fifo" "$server_fifo"

printf '%17s%s\n' '' '_               _'
printf '%3s%s\n' '' '___  _ __ ___| |__   ___  ___| |_ _ __ __ _'
cat <<'EOF'
  / _ \| '__/ __| '_ \ / _ \/ __| __| '__/ _` |
 | (_) | | | (__| | | |  __/\__ \ |_| | | (_| |
  \___/|_|  \___|_| |_|\___||___/\__|_|  \__,_|

EOF

colorize_output() {
    trap '' INT
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *[Ww][Aa][Rr][Nn][Ii][Nn][Gg]*)
                printf '%s%s%s\n' "$orange" "$line" "$reset"
                ;;
            *[Ee][Rr][Rr][Oo][Rr]*)
                printf '%s%s%s\n' "$red" "$line" "$reset"
                ;;
            *)
                printf '%s\n' "$line"
                ;;
        esac
    done
}

warning() {
    printf '%sWARNING: %s%s\n' "$orange" "$1" "$reset"
}

error() {
    printf '%sERROR: %s%s\n' "$red" "$1" "$reset" >&2
}

open_url() {
    url=$1
    case "$(uname -s)" in
        Darwin*) command -v open >/dev/null 2>&1 && open "$url" >/dev/null 2>&1 ;;
        Linux*) command -v xdg-open >/dev/null 2>&1 && xdg-open "$url" >/dev/null 2>&1 ;;
        CYGWIN*|MINGW*|MSYS*) command -v cmd.exe >/dev/null 2>&1 && cmd.exe /c start '' "$url" >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

open_when_ready() {
    url=$1
    attempts=0
    while [ "$attempts" -lt 50 ]; do
        if "$python" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=0.2).read()' "$url" >/dev/null 2>&1; then
            printf 'Opening Orchestra at %s\n' "$url"
            if ! open_url "$url"; then
                warning "Could not open a browser. Open $url manually."
            fi
            return
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    warning "Workstream Manager did not become reachable at $url."
}

run_orc() {
    "$python" -u -c 'import os, sys; sys.path.insert(0, os.environ["ORCHESTRA_SOURCE"]); from orchestration.cli import main; main()' "$@"
}

stop_output_reader() {
    output_pid=$1
    attempts=0
    while kill -0 "$output_pid" 2>/dev/null && [ "$attempts" -lt 50 ]; do
        attempts=$((attempts + 1))
        sleep 0.01
    done
    if kill -0 "$output_pid" 2>/dev/null; then
        kill "$output_pid" 2>/dev/null || true
    fi
    wait "$output_pid" 2>/dev/null || true
}

cleanup() {
    trap - EXIT INT TERM
    if [ -n "$scheduler_pid" ] && kill -0 "$scheduler_pid" 2>/dev/null; then
        kill "$scheduler_pid" 2>/dev/null || true
        wait "$scheduler_pid" 2>/dev/null || true
    fi
    if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    if [ -n "$scheduler_output_pid" ]; then
        stop_output_reader "$scheduler_output_pid"
    fi
    if [ -n "$server_output_pid" ]; then
        stop_output_reader "$server_output_pid"
    fi
    if [ -n "$browser_pid" ] && kill -0 "$browser_pid" 2>/dev/null; then
        kill "$browser_pid" 2>/dev/null || true
        wait "$browser_pid" 2>/dev/null || true
    fi
    rm -rf "$output_dir"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

colorize_output <"$scheduler_fifo" &
scheduler_output_pid=$!
"$python" -u -c 'import os, sys; sys.path.insert(0, os.environ["ORCHESTRA_SOURCE"]); from orchestration.cli import main; main()' \
    --base-dir "$workspace" scheduler run >"$scheduler_fifo" 2>&1 &
scheduler_pid=$!

url="http://localhost:$port"
open_when_ready "$url" &
browser_pid=$!
colorize_output <"$server_fifo" &
server_output_pid=$!
"$python" -u -c 'import os, sys; sys.path.insert(0, os.environ["ORCHESTRA_SOURCE"]); from orchestration.cli import main; main()' \
    --base-dir "$workspace" worksm start --port "$port" --no-open >"$server_fifo" 2>&1 &
server_pid=$!

while kill -0 "$scheduler_pid" 2>/dev/null && kill -0 "$server_pid" 2>/dev/null; do
    sleep 0.1
done

if ! kill -0 "$scheduler_pid" 2>/dev/null; then
    scheduler_status=0
    wait "$scheduler_pid" || scheduler_status=$?
    scheduler_pid=
    error "Scheduler stopped unexpectedly. Shutting down Workstream Manager."
    if [ "$scheduler_status" -eq 0 ]; then
        scheduler_status=1
    fi
    exit "$scheduler_status"
fi

server_status=0
wait "$server_pid" || server_status=$?
server_pid=
if [ "$server_status" -eq 0 ]; then
    wait "$browser_pid" 2>/dev/null || true
fi
exit "$server_status"