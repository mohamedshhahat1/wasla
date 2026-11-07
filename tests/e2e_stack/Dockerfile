# syntax=docker/dockerfile:1
#
# The e2e stack's image (FE-0, E2E-01): the real application, its tests and the
# fakes, installed exactly the way ci.yml installs them. TEST-ONLY - it carries
# the test suite and public test secrets come with the compose file; never
# deploy it. The production image is the repository's own Dockerfile, which
# this one does not change.
#
# Built from the repository root with this file's own ignore file
# (Dockerfile.dockerignore beside it), which - unlike the root .dockerignore -
# keeps tests/ in the context.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 wasla \
 && useradd --system --uid 1001 --gid wasla --home-dir /app wasla

# What ci.yml does: the locks by hash first, then the project and its dev tools,
# which leaves every locked package at its pin - and then the drift guard.
COPY pyproject.toml README.md requirements.lock requirements-build.lock ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --require-hashes -r requirements.lock -r requirements-build.lock \
 && pip install -e ".[dev]" \
 && python -m scripts.check_installed_lock requirements.lock requirements-build.lock \
 && pip check

COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY tests ./tests

RUN mkdir -p /var/lib/wasla/media /mailbox \
 && chown -R wasla:wasla /app /var/lib/wasla /mailbox
USER wasla
