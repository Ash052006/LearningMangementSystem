#!/bin/sh
# Container entrypoint: bring the database up to date, optionally load the demo
# data, then run the command the container was started with (gunicorn by
# default).  `exec` replaces the shell so the server becomes PID 1 and receives
# stop signals from Docker.
set -e

python manage.py migrate --noinput

# SEED_DEMO=1 loads the demo accounts and courses; seed_demo is idempotent, so
# running it on every start is safe.  Unset (or anything else) skips it.
if [ "${SEED_DEMO:-0}" = "1" ]; then
    python manage.py seed_demo
fi

exec "$@"
