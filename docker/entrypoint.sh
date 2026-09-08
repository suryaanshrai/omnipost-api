#!/bin/sh
set -eu

# ROLE selects which process this container runs. Each role is its own
# process (no `&` backgrounding) so one crashing doesn't take another down —
# the old CMD backgrounded the worker with a bare `&` under the runserver's
# PID 1, so a dead worker went unnoticed.
ROLE="${OMNIPOST_ROLE:-web}"

case "$ROLE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput --clear
    exec gunicorn app.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --access-logfile - \
      --error-logfile -
    ;;
  worker)
    exec python manage.py rqworker --with-scheduler default publish publish_low media metrics maintenance ai
    ;;
  reconciler)
    exec python manage.py reconcile_loop
    ;;
  recurrence)
    exec python manage.py recur_loop
    ;;
  token-refresher)
    exec python manage.py refresh_channel_tokens
    ;;
  metrics-poller)
    exec python manage.py metrics_poll_loop
    ;;
  migrate)
    exec python manage.py migrate --noinput
    ;;
  *)
    echo "Unknown OMNIPOST_ROLE: $ROLE" >&2
    exit 1
    ;;
esac
