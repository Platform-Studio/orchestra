#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"$script_dir/install.sh"
"$script_dir/example.sh" "${1:-xmas_movies}"