#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-python}

"$python" -m pip install -e "$script_dir"