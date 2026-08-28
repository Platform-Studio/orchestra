#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
summary_dir=$(mktemp -d "${TMPDIR:-/tmp}/orchestra-setup.XXXXXX")
warnings_file="$summary_dir/warnings"
errors_file="$summary_dir/errors"
status_file="$summary_dir/status"
: >"$warnings_file"
: >"$errors_file"

cleanup() {
    rm -rf "$summary_dir"
}
trap cleanup EXIT HUP INT TERM

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    orange=$(printf '\033[38;5;208m')
    red=$(printf '\033[31m')
    reset=$(printf '\033[0m')
else
    orange=''
    red=''
    reset=''
fi

printf '%17s%s\n' '' '_               _'
printf '%3s%s\n' '' '___  _ __ ___| |__   ___  ___| |_ _ __ __ _'
cat <<'EOF'
  / _ \| '__/ __| '_ \ / _ \/ __| __| '__/ _` |
 | (_) | | | (__| | | |  __/\__ \ |_| | | (_| |
  \___/|_|  \___|_| |_|\___||___/\__|_|  \__,_|

EOF

stream_output() {
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            WARNING:*)
                printf '%s\n' "$line" >>"$warnings_file"
                printf '%s%s%s\n' "$orange" "$line" "$reset"
                ;;
            ERROR:*)
                printf '%s\n' "$line" >>"$errors_file"
                printf '%s%s%s\n' "$red" "$line" "$reset"
                ;;
            *)
                printf '%s\n' "$line"
                ;;
        esac
    done
}

run_step() {
    : >"$status_file"
    {
        set +e
        "$@"
        command_status=$?
        printf '%s\n' "$command_status" >"$status_file"
    } 2>&1 | stream_output
    command_status=$(cat "$status_file")
    if [ "$command_status" -ne 0 ]; then
        error_message="ERROR: Command failed with exit code $command_status: $*"
        printf '%s\n' "$error_message" >>"$errors_file"
        printf '%s%s%s\n' "$red" "$error_message" "$reset"
        return "$command_status"
    fi
}

print_summary() {
    if [ -s "$warnings_file" ]; then
        printf '\n%sWarnings:%s\n' "$orange" "$reset"
        while IFS= read -r line; do
            printf '%s%s%s\n' "$orange" "$line" "$reset"
        done <"$warnings_file"
    fi
    if [ -s "$errors_file" ]; then
        printf '\n%sErrors:%s\n' "$red" "$reset"
        while IFS= read -r line; do
            printf '%s%s%s\n' "$red" "$line" "$reset"
        done <"$errors_file"
    fi
}

# Create a venv if one isn't active, so the install stays out of the system Python.
if [ -z "${VIRTUAL_ENV:-}" ]; then
    venv_dir="$script_dir/.venv"
    if [ ! -d "$venv_dir" ]; then
        printf 'Creating virtual environment at %s …\n' "$venv_dir"
        python3 -m venv "$venv_dir"
    fi
    # shellcheck disable=SC1091
    . "$venv_dir/bin/activate"
fi

setup_status=0
if run_step "$script_dir/install.sh"; then
    if run_step "$script_dir/example.sh" "${1:-xmas_movies}"; then
        :
    else
        setup_status=$?
    fi
else
    setup_status=$?
fi

print_summary

if [ "$setup_status" -eq 0 ]; then
    printf '\nOrchestra installed successfully.\n'
    printf 'Open the Workstream Manager with ./run-orchestra.sh\n'
fi

exit "$setup_status"