# syntax=docker/dockerfile:1

# Slim Python image; every dependency we install ships wheels, so no build
# toolchain is needed on top of it.
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE keeps .pyc files out of the image, PYTHONUNBUFFERED
# makes `print`/logs show up immediately, and the pip flags avoid a cache and a
# version-check network call during the build.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run the app as an unprivileged user.  /data is the mount point for the SQLite
# database (see docker-compose.yml) and is pre-created and owned by the app
# user so it stays writable once the volume is attached.
RUN useradd --create-home --uid 1000 app \
    && install -d -o app -g app /data

# Install dependencies first, so this layer stays cached until requirements.txt
# actually changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project and collect the static files into STATIC_ROOT at build time;
# WhiteNoise serves them from there at runtime.  The tree is then handed to the
# app user so nothing needs to write to it as root.
COPY . .

# collectstatic imports the settings, so it needs a SECRET_KEY even at build
# time.  A throw-away build-only value is enough: an ARG is not baked into the
# image environment (only ENV would be) and nothing in this step signs
# anything.  The real key must be supplied at run time, e.g. by
# docker-compose.yml, and the app refuses to start without one.
ARG SECRET_KEY=build-time-only-not-persisted
RUN python manage.py collectstatic --noinput \
    && chown -R app:app /app

# Entrypoint is a small script so `docker run` also migrates the database, and
# `exec` keeps gunicorn as PID 1 so it receives stop signals.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
# Gunicorn fronts Django; WhiteNoise (in the middleware) serves the static files
# it collects, so gunicorn needs no extra handler.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
