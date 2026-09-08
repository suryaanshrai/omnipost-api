FROM python:3.12.3-slim

# Set by CI (see .github/workflows/docker-image.yml) to the commit this image
# was built from — read by settings.py as the Sentry release tag, so an
# issue traces back to the exact image, the same SHA it's tagged with for
# rollback.
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app

# libmagic1 is required by python-magic for the media MIME sniffing in models.py.
# ffmpeg provides ffprobe (metadata ingest) and ffmpeg (rendition transcoding) — jobs/media.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==1.8.2

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --no-root --only main

COPY app .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home --uid 1000 omnipost \
    && mkdir -p /app/staticfiles /app/mediafiles \
    && chown -R omnipost:omnipost /app
USER omnipost

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
