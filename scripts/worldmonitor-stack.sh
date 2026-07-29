#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
submodule_dir="$project_dir/external/worldmonitor"
expected_commit="6c48a33c97cd643d87ee3a4ed2b54aacbb1cbc3b"
base_compose="$project_dir/docker/docker-compose.yml"
wm_compose="$project_dir/docker/docker-compose.worldmonitor.yml"

compose() {
  profile_args=""
  ais_key=$(sed -n 's/^AISSTREAM_API_KEY=//p' "$project_dir/.env" | tail -n 1)
  if [ -n "$ais_key" ]; then
    profile_args="--profile ais"
  fi
  # shellcheck disable=SC2086
  docker compose --env-file "$project_dir/.env" \
    -f "$base_compose" -f "$wm_compose" $profile_args "$@"
}

validate() {
  if [ ! -f "$project_dir/.env" ]; then
    echo "Missing $project_dir/.env; copy .env.example and configure it first." >&2
    exit 2
  fi
  if [ ! -f "$submodule_dir/.git" ]; then
    echo "World Monitor submodule is not initialized." >&2
    echo "Run: git submodule update --init --recursive" >&2
    exit 2
  fi
  actual_commit=$(git -C "$submodule_dir" rev-parse HEAD)
  if [ "$actual_commit" != "$expected_commit" ]; then
    echo "World Monitor commit mismatch: expected $expected_commit, got $actual_commit" >&2
    exit 2
  fi
  for key in RELAY_SHARED_SECRET REDIS_PASSWORD REDIS_TOKEN; do
    value=$(sed -n "s/^${key}=//p" "$project_dir/.env" | tail -n 1)
    if [ -z "$value" ]; then
      echo "Missing required $key in $project_dir/.env" >&2
      exit 2
    fi
  done
  compose config >/dev/null
  echo "World Monitor integration configuration is valid."
}

command="${1:-}"
case "$command" in
  validate)
    validate
    ;;
  up)
    validate
    compose up -d --build
    ;;
  down)
    compose down
    ;;
  status)
    compose ps
    ;;
  logs)
    shift
    if [ "$#" -gt 0 ]; then
      compose logs -f "$@"
    else
      compose logs -f
    fi
    ;;
  seed)
    validate
    compose run --rm worldmonitor-seeder --once
    ;;
  *)
    echo "Usage: $0 {validate|up|down|status|logs|seed}" >&2
    exit 2
    ;;
esac
