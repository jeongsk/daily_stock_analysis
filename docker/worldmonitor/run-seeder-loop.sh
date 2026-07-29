#!/bin/sh
set -eu

interval="${WORLDMONITOR_SEED_INTERVAL_SECONDS:-1800}"
case "$interval" in
  ''|*[!0-9]*|0)
    echo "WORLDMONITOR_SEED_INTERVAL_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac

run_seeders() {
  echo "[worldmonitor-seeder] starting seed run"
  if ./scripts/run-seeders.sh; then
    echo "[worldmonitor-seeder] seed run completed"
  else
    echo "[worldmonitor-seeder] seed run completed with failures" >&2
  fi
}

if [ "${1:-}" = "--once" ]; then
  run_seeders
  exit 0
fi

while true; do
  run_seeders
  sleep "$interval"
done
