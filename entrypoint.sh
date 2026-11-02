#!/bin/sh
set -eu

python manage.py migrate --noinput

if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    python manage.py createsuperuser --noinput || true
fi

exec gunicorn vimple.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 1 --threads 4
