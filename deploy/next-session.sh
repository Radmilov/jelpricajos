#!/usr/bin/env bash
# Set or clear the "next session" countdown shown on the site (run on the server).
#
#   sudo bash /opt/jelpricajos/deploy/next-session.sh "2026-11-19 10:00"   # Serbian time
#   sudo bash /opt/jelpricajos/deploy/next-session.sh clear
#
# A stream scheduled on the YouTube channel, if there ever is one, takes precedence.
set -euo pipefail

[[ $# -eq 1 ]] || { sed -n '2,7p' "$0"; exit 1; }

if [[ "$1" == "clear" ]]; then
  body='{"at": null}'
else
  # Interpret the given time as Serbian time, whatever the server's time zone is.
  at=$(TZ=Europe/Belgrade date -d "$1" --iso-8601=seconds) \
    || { echo "Could not understand the time: $1 (use e.g. \"2026-11-19 10:00\")" >&2; exit 1; }
  body="{\"at\": \"${at}\"}"
fi

curl -fsS -X POST http://127.0.0.1:8000/api/admin/next-session \
  -H 'Content-Type: application/json' -d "$body"
echo
