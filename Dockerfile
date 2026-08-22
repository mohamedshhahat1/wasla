# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app


FROM base AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .


FROM base AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 wasla \
 && useradd --system --uid 1001 --gid wasla --home-dir /app wasla

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

# The media directory is created here, owned by the application user, rather
# than left for the volume mount to make. A named volume mounted onto a path
# that does not exist in the image is created owned by root, and this container
# runs as `wasla` - so every attempt to store a customer's attachment would
# fail on permissions, in the worker, at run time, and nowhere earlier.
RUN chmod +x scripts/entrypoint.sh \
 && mkdir -p /var/lib/wasla/media \
 && chown -R wasla:wasla /app /var/lib/wasla
USER wasla

EXPOSE 8000

# Liveness only: readiness depends on PostgreSQL and Redis, which must not
# cause the container itself to be restarted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["api"]
