#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-python}

if [ "$#" -lt 1 ]; then
    printf 'Usage: %s <example> [setup arguments...]\n' "$0" >&2
    printf 'Available examples: fruit_and_veg, xmas_movies\n' >&2
    exit 2
fi

example_name=$1
shift

case "$example_name" in
    *[!A-Za-z0-9_]*|'')
        printf 'Invalid example name: %s\n' "$example_name" >&2
        exit 2
        ;;
esac

setup_script="$script_dir/examples/$example_name/setup.py"
if [ ! -f "$setup_script" ]; then
    printf 'Unknown example: %s\n' "$example_name" >&2
    printf 'Available examples: fruit_and_veg, xmas_movies\n' >&2
    exit 2
fi

exec "$python" "$setup_script" "$@"