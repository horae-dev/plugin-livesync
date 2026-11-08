# syntax=docker/dockerfile:1
# The LiveSync sidecar: the official Self-hosted LiveSync CLI (vrtmrz/obsidian-livesync,
# src/apps/cli, MIT) supervised by `horae-livesync` (this repository). See README "Sidecar".
#
#   docker build -t horae-livesync .
#
# The CLI is taken unchanged from its official image, pinned by digest: its Node.js runtime and
# its `/app` bundle (dist + production node_modules, built on the same Debian bookworm as the
# base below). Bump LIVESYNC_CLI (tag and digest) and LIVESYNC_CLI_VERSION together.
ARG LIVESYNC_CLI=ghcr.io/vrtmrz/livesync-cli:1.0.46-patch2-cli@sha256:4a0675db1ebd6d891624daedb37b1467f60de7b1a090f0252b7a1769fb382701
ARG PYTHON=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG UV=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${LIVESYNC_CLI} AS cli
FROM ${UV} AS uv

# Build the venv from uv.lock (git is needed for the horae-sdk source; build stage only).
FROM ${PYTHON} AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LOCKED=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev --no-editable --python /usr/local/bin/python3

FROM ${PYTHON}
ARG LIVESYNC_CLI_VERSION=1.0.46-patch2-cli
COPY --from=cli /usr/local/bin/node /usr/local/bin/node
COPY --from=cli /app /app
COPY --from=build /opt/venv /opt/venv
# One user, 10001 like the Horae image, so the shared vault volume has one owner. The three
# mount points are created 0700 and owned by it: a new named volume mounted there starts so.
RUN groupadd --gid 10001 horae \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin horae \
    && install -d -o 10001 -g 10001 -m 0700 /vault /livesync /status \
    && node /app/dist/index.cjs --help > /dev/null
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin HOME=/tmp PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 LIVESYNC_CLI_VERSION=${LIVESYNC_CLI_VERSION}
LABEL org.opencontainers.image.title="horae-livesync" \
      org.opencontainers.image.source="https://github.com/horae-dev/plugin-livesync" \
      org.opencontainers.image.licenses="Apache-2.0 AND MIT" \
      dev.horae.livesync-cli.version="${LIVESYNC_CLI_VERSION}"
USER 10001:10001
# Mounts: /config (livesync.yaml, read-only), /run/secrets (read-only), /vault, /livesync (the
# CLI's local database, persistent), /status (shared read-only with Horae), /tmp (a tmpfs:
# the rendered settings live there, 0600).
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["horae-livesync", "health"]
ENTRYPOINT ["horae-livesync"]
CMD ["run"]
