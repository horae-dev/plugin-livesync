# syntax=docker/dockerfile:1
# The LiveSync sidecar: the official Self-hosted LiveSync CLI (vrtmrz/obsidian-livesync,
# src/apps/cli, MIT) supervised by `horae-livesync` (this repository). See README "Sidecar".
#
#   docker build --platform linux/amd64 -t horae-livesync .
#
# linux/amd64 only: the platform CI builds and tests (README, "Deploying").
#
# The CLI is taken unchanged from its official image, pinned by digest: its Node.js runtime and
# its `/app` bundle (dist + production node_modules, built on the same Debian bookworm as the
# base below). Bump LIVESYNC_CLI (tag and digest), LIVESYNC_CLI_VERSION and LIVESYNC_COMMIT
# together, and refresh licenses/obsidian-livesync.LICENSE and THIRD_PARTY_NOTICES.
ARG LIVESYNC_CLI=ghcr.io/vrtmrz/livesync-cli:1.0.46-patch2-cli@sha256:4a0675db1ebd6d891624daedb37b1467f60de7b1a090f0252b7a1769fb382701
ARG PYTHON=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
# The build stage only: the same Python 3.12 on the full bookworm image, so nothing is
# installed from apt at build time.
ARG PYTHON_BUILD=python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267
ARG UV=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${LIVESYNC_CLI} AS cli
FROM ${UV} AS uv

# Build the venv from uv.lock.
FROM ${PYTHON_BUILD} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LOCKED=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev --no-editable --python /usr/local/bin/python3

FROM ${PYTHON}
ARG LIVESYNC_CLI_VERSION=1.0.46-patch2-cli
ARG LIVESYNC_COMMIT=c2e4e9198b01a58be72d9b76fd6f2661aeb01ec8
COPY --from=cli /usr/local/bin/node /usr/local/bin/node
COPY --from=cli /app /app
COPY --from=build /opt/venv /opt/venv
# Licences (THIRD_PARTY_NOTICES lists what is in the image and where each licence is).
COPY --from=cli /usr/local/LICENSE /usr/share/licenses/node/LICENSE
COPY licenses/obsidian-livesync.LICENSE /usr/share/licenses/obsidian-livesync/LICENSE
COPY LICENSE THIRD_PARTY_NOTICES /usr/share/licenses/horae-livesync/
# One user, 10001 like the Horae image, so the shared vault volume has one owner. The four
# mount points are created 0700 and owned by it: a new named volume mounted there starts so.
RUN groupadd --gid 10001 horae \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin horae \
    && install -d -o 10001 -g 10001 -m 0700 /vault /livesync /status /snapshots \
    && node /app/dist/index.cjs --help > /dev/null
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin HOME=/tmp PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 LIVESYNC_CLI_VERSION=${LIVESYNC_CLI_VERSION}
# The image's own code is Apache-2.0; with it come Node.js and the LiveSync CLI (MIT) and the
# CLI's npm dependencies (MPL-2.0 among them), Python (PSF-2.0) and Debian packages:
# /usr/share/licenses/horae-livesync/THIRD_PARTY_NOTICES.
LABEL org.opencontainers.image.title="horae-livesync" \
      org.opencontainers.image.source="https://github.com/horae-dev/plugin-livesync" \
      org.opencontainers.image.licenses="Apache-2.0 AND MIT AND MPL-2.0 AND PSF-2.0 AND BSD-3-Clause AND BSD-2-Clause AND ISC AND BlueOak-1.0.0 AND Unlicense AND 0BSD" \
      dev.horae.livesync-cli.version="${LIVESYNC_CLI_VERSION}" \
      dev.horae.livesync-cli.commit="${LIVESYNC_COMMIT}"
USER 10001:10001
# Mounts: /config (livesync.yaml, read-only), /run/secrets (read-only), /vault, /livesync (the
# CLI's local database, persistent), /status (shared read-only with Horae), /snapshots (the
# git snapshots of the vault, persistent), /tmp (a tmpfs:
# the rendered settings live there, 0600).
# The thresholds come from livesync.yaml (written into the status). The start period covers the
# first fetch of a small vault; raise it for a large one (README, "Deploying").
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD ["horae-livesync", "health"]
ENTRYPOINT ["horae-livesync"]
CMD ["run"]
