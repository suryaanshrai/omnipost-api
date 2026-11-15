#!/bin/sh
# Live checks against the real platforms (app/connectors/tests/live).
# Reads credentials from .env.live (copy .env.live.example). Extra
# arguments go to pytest, e.g.  ./scripts/live_test.sh -k "x or mastodon"
set -eu
cd "$(dirname "$0")/.."
if [ -f .env.live ]; then
  set -a; . ./.env.live; set +a
else
  echo "No .env.live found: copy .env.live.example and fill in credentials." >&2
fi
: "${DJANGO_SECRET_KEY:=live-tests}" "${OMNIPOST_KEK:=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=}"
export DJANGO_SECRET_KEY OMNIPOST_KEK
RUNNER="python -m pytest"
command -v poetry >/dev/null 2>&1 && RUNNER="poetry run pytest"
exec $RUNNER -m live -v -rs -s app/connectors/tests/live "$@"
