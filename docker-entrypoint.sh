#!/usr/bin/env bash
# One container, two processes: the web app (gunicorn) and the background
# worker. If either stops, the other is stopped too and the container exits,
# so Docker's restart policy brings both back together.
set -euo pipefail

python manage.py init-db
python manage.py ensure-admin

python worker.py &
worker=$!
gunicorn -c gunicorn.conf.py dashboard.app:app &
web=$!

shutdown() {
  kill -TERM "$web" "$worker" 2>/dev/null || true
  wait "$web" "$worker" 2>/dev/null || true
}
trap shutdown TERM INT

wait -n "$web" "$worker"
status=$?
shutdown
exit "$status"
