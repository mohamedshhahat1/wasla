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

# What ci.yml does: the locks by hash first, then the project's dev tools, which
# leaves every locked package at its pin - and then the drift guard. Ordered so a
# code change reuses both dependency layers (ADR-201): the locks from the lock
# files alone, the dev tools from pyproject.toml's own list (resolved as ci.yml
# resolves them, unhashed - FEB-43), and only then the code, and the package
# without its dependencies.
COPY requirements.lock requirements-build.lock ./
RUN pip install --require-hashes -r requirements.lock -r requirements-build.lock
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']))" > /tmp/dev-requirements.txt \
 && pip install -r /tmp/dev-requirements.txt \
 && rm /tmp/dev-requirements.txt
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-deps -e . \
 && python -m scripts.check_installed_lock requirements.lock requirements-build.lock \
 && pip check

COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY tests ./tests

RUN mkdir -p /var/lib/wasla/media /mailbox \
 && chown -R wasla:wasla /app /var/lib/wasla /mailbox
USER wasla
