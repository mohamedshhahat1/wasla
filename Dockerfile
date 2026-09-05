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

# Provenance. A container answering a pager at three in the morning has to be
# traceable to the commit that built it, and `docker inspect` is the only place
# an operator can look without the deployment pipeline's help. Build arguments
# rather than baked constants, so a local build says so instead of lying about
# a revision it does not have.
ARG WASLA_VERSION="0.0.0-local"
ARG WASLA_REVISION="unknown"
ARG WASLA_BUILT_AT="unknown"
LABEL org.opencontainers.image.title="Wasla" \
      org.opencontainers.image.description="Multi-tenant AI customer engagement platform for WhatsApp Business" \
      org.opencontainers.image.source="https://github.com/mohamedshhahat1/wasla" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary" \
      org.opencontainers.image.version="${WASLA_VERSION}" \
      org.opencontainers.image.revision="${WASLA_REVISION}" \
      org.opencontainers.image.created="${WASLA_BUILT_AT}"
# Readable from inside the process too, so a log line or a health response can
# name the build without shelling out to the container runtime.
ENV WASLA_BUILD_REVISION="${WASLA_REVISION}"

# `upgrade` as well as `install`: the base image is rebuilt on its own schedule,
# so between rebuilds it carries whatever security updates Debian has published
# since. Without this the image ships known-fixed CVEs - util-linux alone
# accounted for four of them across nine packages - and the container scan
# fails the release over something a package update fixes.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 wasla \
 && useradd --system --uid 1001 --gid wasla --home-dir /app wasla

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# A production container has no business installing packages, and pip is on no
# path this image runs: the entrypoint calls uvicorn, alembic and `python -m`.
# Removing it also removes the libraries pip vendors, which a scanner reads out
# of its vendor manifest and reports against the image even though nothing
# imports them - msgpack and setuptools were both found that way. Deleting code
# that never runs is a better answer than carrying a package installer into
# production in order to keep it patched.
# The console scripts go with the package. Deleting only `site-packages/pip`
# leaves `/opt/venv/bin/pip` behind as a shim that fails on its first import -
# harmless, and misleading in the way that matters: `command -v pip` succeeds,
# so an operator checking the image is told pip is present when it cannot
# install anything.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /opt/venv/lib/python3.12/site-packages/pip \
           /opt/venv/lib/python3.12/site-packages/pip-*.dist-info \
 && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
          /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.*

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

# The media directory is created here, owned by the application user, rather
# than left for the volume mount to make. A named volume mounted onto a path
# that does not exist in the image is created owned by root, and this container
# runs as `wasla` - so every attempt to store a customer's attachment would
# fail on permissions, in the worker, at run time, and nowhere earlier.
RUN chmod +x scripts/entrypoint.sh scripts/check_readiness.sh \
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
