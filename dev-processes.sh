#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./dev-processes.sh [--all] [--hide-command]

List local development-looking Python, Node, and Nuxt/Vite/Next processes.

Default output includes local Python/Node processes that listen on a TCP port
and look like app servers or frontend dev servers. Use --all to also include
non-listening commands with a strong dev-server signature.

Options:
  --all            Include non-listening commands with a strong dev signature.
  --hide-command   Hide the full COMMAND column for a narrower table.
EOF
}

include_all=0
show_command=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      include_all=1
      ;;
    --hide-command|--no-command)
      show_command=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof is required but was not found." >&2
  exit 1
fi

if ! command -v ps >/dev/null 2>&1; then
  echo "ps is required but was not found." >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

ports_file="$tmp_dir/ports.tsv"
rows_file="$tmp_dir/rows.tsv"

lsof -nP -iTCP -sTCP:LISTEN -FpPn 2>/dev/null | awk '
  /^p/ { pid = substr($0, 2); next }
  /^n/ {
    endpoint = substr($0, 2)
    port = endpoint
    sub(/^.*:/, "", port)
    if (pid != "" && port ~ /^[0-9]+$/) {
      if (ports[pid] == "") {
        ports[pid] = port
        endpoints[pid] = endpoint
      } else if (("," ports[pid] ",") !~ ("," port ",")) {
        ports[pid] = ports[pid] "," port
        endpoints[pid] = endpoints[pid] "," endpoint
      }
    }
  }
  END {
    for (pid in ports) {
      print pid "\t" ports[pid] "\t" endpoints[pid]
    }
  }
' >"$ports_file"

lookup_ports() {
  local pid="$1"
  awk -F '\t' -v wanted="$pid" '$1 == wanted { print $2; found = 1; exit } END { if (!found) print "-" }' "$ports_file"
}

lookup_endpoints() {
  local pid="$1"
  awk -F '\t' -v wanted="$pid" '$1 == wanted { print $3; found = 1; exit } END { if (!found) print "-" }' "$ports_file"
}

process_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

project_for_cwd() {
  local cwd="$1"
  local dir="$cwd"

  if [[ -z "$dir" || ! -d "$dir" ]]; then
    printf '%s\n' "-"
    return
  fi

  while [[ "$dir" == /Users/* && "$dir" != /Users && "$dir" != / ]]; do
    if [[ -d "$dir/.git" || -f "$dir/package.json" || -f "$dir/pyproject.toml" || -f "$dir/docker-compose.yml" ]]; then
      printf '%s\n' "$(basename "$dir")"
      return
    fi
    dir="$(dirname "$dir")"
  done

  printf '%s\n' "-"
}

classify_command() {
  local command_lc="$1"

  if [[ "$command_lc" == *nuxt* ]]; then
    printf '%s\n' "nuxt"
  elif [[ "$command_lc" == *vite* ]]; then
    printf '%s\n' "vite"
  elif [[ "$command_lc" =~ (^|[/[:space:]])next([[:space:]/.]|$) || "$command_lc" == *node_modules/.bin/next* || "$command_lc" == */next/dist/* ]]; then
    printf '%s\n' "next"
  else
    case "$command_lc" in
    *uvicorn*) printf '%s\n' "uvicorn" ;;
    *gunicorn*) printf '%s\n' "gunicorn" ;;
    *flask*) printf '%s\n' "flask" ;;
    *manage.py*runserver*) printf '%s\n' "django" ;;
    *celery*) printf '%s\n' "celery" ;;
    *npm*run*dev*|*pnpm*dev*|*yarn*dev*) printf '%s\n' "node-dev" ;;
    *node*) printf '%s\n' "node" ;;
    *python*) printf '%s\n' "python" ;;
    *) printf '%s\n' "dev" ;;
    esac
  fi
}

is_runtime_command() {
  local command_lc="$1"

  case "$command_lc" in
    *node*|*npm*|*npx*|*pnpm*|*yarn*|*bun*|*nuxt*|*vite*|*next*|*tsx*|*ts-node*|*python*|*uvicorn*|*gunicorn*|*flask*|*django*|*celery*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_strong_dev_command() {
  local command_lc="$1"

  [[ "$command_lc" == *nuxt* \
    || "$command_lc" == *vite* \
    || "$command_lc" =~ (^|[/[:space:]])next([[:space:]/.]|$) \
    || "$command_lc" == *node_modules/.bin/next* \
    || "$command_lc" == *uvicorn* \
    || "$command_lc" == *gunicorn* \
    || "$command_lc" == *flask*run* \
    || "$command_lc" == *manage.py*runserver* \
    || "$command_lc" == *fastapi* \
    || "$command_lc" == *celery* \
    || "$command_lc" == *npm*run*dev* \
    || "$command_lc" == *pnpm*dev* \
    || "$command_lc" == *yarn*dev* \
    || "$command_lc" == *dev_servers.py* \
    || "$command_lc" == *workstream_manager* ]]
}

is_excluded_helper() {
  local command_lc="$1"

  case "$command_lc" in
    */applications/visual\ studio\ code.app/*|*.vscode/extensions/*|*logitech.localized*|*logi_crashpad*|*agents/cli/browser.py\ _server*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_local_process() {
  local command="$1"
  local cwd="$2"

  [[ "$cwd" == /Users/* || "$command" == *"/Users/"* || "$command" == *".venv"* || "$command" == *"node_modules"* ]]
}

ps -axo pid=,ppid=,lstart=,etime=,command= | while read -r pid ppid dow mon day started_time year etime command; do
  [[ -n "${pid:-}" && -n "${command:-}" ]] || continue

  command_lc="$(printf '%s' "$command" | tr '[:upper:]' '[:lower:]')"
  if ! is_runtime_command "$command_lc"; then
    continue
  fi

  if is_excluded_helper "$command_lc"; then
    continue
  fi

  ports="$(lookup_ports "$pid")"
  if [[ "$include_all" -ne 1 && "$ports" == "-" ]]; then
    continue
  fi

  cwd="$(process_cwd "$pid" || true)"
  if ! is_local_process "$command" "$cwd"; then
    continue
  fi

  if ! is_strong_dev_command "$command_lc" && [[ "$ports" == "-" ]]; then
    continue
  fi

  kind="$(classify_command "$command_lc")"
  endpoints="$(lookup_endpoints "$pid")"
  project="$(project_for_cwd "$cwd")"
  started="${dow} ${mon} ${day} ${started_time} ${year}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$pid" "$ppid" "$etime" "$started" "$kind" "$ports" "$project" "${cwd:-}" "$endpoints" "$command"
done | sort -t $'\t' -k7,7 -k6,6 -k1,1n >"$rows_file"

if [[ ! -s "$rows_file" ]]; then
  echo "No local development-looking Python/Node processes found."
  exit 0
fi

if [[ "$show_command" -eq 1 ]]; then
  printf 'PID\tPPID\tRUNTIME\tSTARTED\tKIND\tPORTS\tPROJECT\tCWD\tLISTENERS\tCOMMAND\n' >"$tmp_dir/header.tsv"
  cat "$tmp_dir/header.tsv" "$rows_file" | column -t -s $'\t'
else
  printf 'PID\tPPID\tRUNTIME\tSTARTED\tKIND\tPORTS\tPROJECT\tCWD\tLISTENERS\n' >"$tmp_dir/header.tsv"
  cut -f 1-9 "$rows_file" >"$tmp_dir/rows-without-command.tsv"
  cat "$tmp_dir/header.tsv" "$tmp_dir/rows-without-command.tsv" | column -t -s $'\t'
fi