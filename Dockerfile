# --- build: resolve dependencies with Poetry, install them into a venv ---
FROM python:3.12-slim AS build

RUN pip install --no-cache-dir poetry==1.8.5 poetry-plugin-export==1.8.0
WORKDIR /src
COPY pyproject.toml poetry.lock ./
RUN poetry export --only main --format requirements.txt --output requirements.txt \
    && python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime: no Poetry, no build tools ---
FROM python:3.12-slim

# Set by CI to the commit this image was built from; settings.py uses it as
# the Sentry release, and images are tagged with the same SHA for rollback.
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA} \
    PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# libmagic1: python-magic's MIME sniffing. ffmpeg: media probing/transcoding
# (jobs/media.py). curl: the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /venv /venv
WORKDIR /app
COPY app .
COPY docker/entrypoint.sh /entrypoint.sh

# Static files are collected at build time (they only depend on the code),
# with throwaway settings values that never reach the running container.
RUN DJANGO_DEBUG=true DJANGO_SECRET_KEY=build-only OMNIPOST_KEK=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
    python manage.py collectstatic --noinput \
    && chmod +x /entrypoint.sh \
    && useradd --create-home --uid 1000 omnipost \
    && mkdir -p /app/mediafiles \
    && chown -R omnipost:omnipost /app/mediafiles
USER omnipost

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
