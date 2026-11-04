#!/bin/sh
set -eu

# OMNIPOST_ROLE selects which process this container runs. Each role is its
# own process so one crashing doesn't take another down.
ROLE="${OMNIPOST_ROLE:-web}"

case "$ROLE" in
  web)
    # docker-compose runs migrations once in a `migrate` service and sets
    # MIGRATE_ON_START=false; a single-container deploy migrates here.
    if [ "${MIGRATE_ON_START:-true}" = "true" ]; then
      python manage.py migrate --noinput
    fi
    exec gunicorn app.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-150}" \
      --access-logfile - \
      --error-logfile -
    ;;
  worker)
    # Every queue in one process (simplest; what older compose files use).
    exec python manage.py rqworker --with-scheduler publish maintenance default media metrics ai
    ;;
  worker-publish)
    # Time-sensitive work only, so a video transcode never delays a publish.
    # Pair with worker-media.
    exec python manage.py rqworker --with-scheduler publish maintenance default
    ;;
  worker-media)
    exec python manage.py rqworker media metrics ai
    ;;
  scheduler)
    exec python manage.py run_scheduler
    ;;
  # Older compose files ran each periodic task in its own container.
  reconciler)
    exec python manage.py run_scheduler --only reconcile
    ;;
  recurrence)
    exec python manage.py run_scheduler --only recurrence
    ;;
  token-refresher)
    exec python manage.py run_scheduler --only token-refresh
    ;;
  metrics-poller)
    exec python manage.py run_scheduler --only metrics
    ;;
  migrate)
    exec python manage.py migrate --noinput
    ;;
  *)
    echo "Unknown OMNIPOST_ROLE: $ROLE" >&2
    exit 1
    ;;
esac
