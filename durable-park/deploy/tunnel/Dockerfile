# withHuman gateway tunnel + four stdio MCP servers:
#   - Intuit QuickBooks Online MCP server (`qbo-mcp-server`)
#   - TapDash Maverick MCP server (`maverick-mcp-server`)
#   - TapDash accept.blue MCP server (`acceptblue-mcp-server`)
#   - TapDash Authorize.net MCP server (`authnet-mcp-server`)
# The tunnel runs each one as a child process (see tunnel.yaml).
# Build with build.sh (it checks the three TapDash checkouts first), not with a bare `docker compose build`.
ARG TUNNEL_IMAGE=quay.io/withhuman/tunnel:0.0.369@sha256:dad0c8b52d653b4969161a8ec589cf37048da6eec5747e098969e6a281f0c3e0

# ---- build stage: fetch + compile the Intuit server at a pinned commit ----
# Uses the tunnel image itself as the builder so Node/libc match the runtime.
FROM ${TUNNEL_IMAGE} AS build
ARG QBO_REPO=https://github.com/intuit/quickbooks-online-mcp-server
ARG QBO_SHA=31a1dd5c3e170e368dbff651abbaf61aabde6ed9
USER root
RUN apk add --no-cache git
WORKDIR /src
RUN git init -q . \
 && git remote add origin "${QBO_REPO}" \
 && git fetch -q --depth 1 origin "${QBO_SHA}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${QBO_SHA}"
# `npm ci` runs the package's `prepare` script, which runs `tsc` -> dist/
RUN npm ci --no-audit --no-fund \
 && test -f dist/index.js \
 && npm prune --omit=dev --no-audit --no-fund \
 && rm -rf .git tests docs

# ---- maverick stage: the Maverick server in its own venv, every download hash-checked ----
# Source: the build context `maverick-src` (docker-compose.yml), the read-only checkout at the commit in
# maverick-mcp.sha; build.sh refuses to build when that checkout is another commit or has local changes.
# Same tunnel image as the builder, so the venv's Python (the image's /usr/bin/python3) matches the runtime.
FROM ${TUNNEL_IMAGE} AS maverick
USER root
COPY --from=maverick-src requirements.lock requirements-dev.lock pyproject.toml /src/maverick/
COPY --from=maverick-src src /src/maverick/src
# 1. Build venv (thrown away): the dev lock, hash-checked, gives the pinned setuptools; it builds the wheel with
#    no network and no build isolation, and fails if a build dependency is missing or the wrong version.
# 2. Runtime venv: only the hashed runtime lock (wheels only), then the wheel, without dependencies or an index.
RUN python3 -m venv /opt/maverick-build \
 && /opt/maverick-build/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/maverick/requirements-dev.lock \
 && /opt/maverick-build/bin/pip wheel --no-cache-dir --no-deps --no-build-isolation --check-build-dependencies -w /src/wheels /src/maverick \
 && python3 -m venv /opt/maverick-mcp \
 && /opt/maverick-mcp/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/maverick/requirements.lock \
 && /opt/maverick-mcp/bin/pip install --no-cache-dir --no-deps --no-index /src/wheels/maverick_mcp-*.whl \
 && /opt/maverick-mcp/bin/python -m maverick_mcp --list-approvals > /dev/null

# ---- acceptblue stage: the accept.blue server, built exactly like the Maverick one ----
# Source: the build context `acceptblue-src`, root's read-only checkout at the commit in acceptblue-mcp.sha.
FROM ${TUNNEL_IMAGE} AS acceptblue
USER root
COPY --from=acceptblue-src requirements.lock requirements-dev.lock pyproject.toml /src/acceptblue/
COPY --from=acceptblue-src src /src/acceptblue/src
RUN python3 -m venv /opt/acceptblue-build \
 && /opt/acceptblue-build/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/acceptblue/requirements-dev.lock \
 && /opt/acceptblue-build/bin/pip wheel --no-cache-dir --no-deps --no-build-isolation --check-build-dependencies -w /src/wheels /src/acceptblue \
 && python3 -m venv /opt/acceptblue-mcp \
 && /opt/acceptblue-mcp/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/acceptblue/requirements.lock \
 && /opt/acceptblue-mcp/bin/pip install --no-cache-dir --no-deps --no-index /src/wheels/acceptblue_mcp-*.whl \
 && /opt/acceptblue-mcp/bin/python -m acceptblue_mcp --list-approvals > /dev/null

# ---- authnet stage: the Authorize.net server, built exactly like the Maverick one ----
# Source: the build context `authnet-src`, root's read-only checkout at the commit in authorizenet-mcp.sha.
# Only the stdio server is installed and started; its optional webhook receiver is not run (nothing sets
# the receiver's listen address or signature key).
FROM ${TUNNEL_IMAGE} AS authnet
USER root
COPY --from=authnet-src requirements.lock requirements-dev.lock pyproject.toml /src/authnet/
COPY --from=authnet-src src /src/authnet/src
RUN python3 -m venv /opt/authnet-build \
 && /opt/authnet-build/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/authnet/requirements-dev.lock \
 && /opt/authnet-build/bin/pip wheel --no-cache-dir --no-deps --no-build-isolation --check-build-dependencies -w /src/wheels /src/authnet \
 && python3 -m venv /opt/authnet-mcp \
 && /opt/authnet-mcp/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /src/authnet/requirements.lock \
 && /opt/authnet-mcp/bin/pip install --no-cache-dir --no-deps --no-index /src/wheels/authorizenet_mcp-*.whl \
 && /opt/authnet-mcp/bin/python -m authorizenet_mcp --list-approvals > /dev/null

# ---- runtime stage: the unchanged tunnel image + all four servers ----
FROM ${TUNNEL_IMAGE}
USER root
COPY --from=build /src /opt/qbo-mcp
COPY --from=maverick /opt/maverick-mcp /opt/maverick-mcp
COPY --from=acceptblue /opt/acceptblue-mcp /opt/acceptblue-mcp
COPY --from=authnet /opt/authnet-mcp /opt/authnet-mcp
RUN ln -s /opt/qbo-mcp/dist/index.js /usr/local/bin/qbo-mcp-server \
 && printf '%s\n' '#!/bin/sh' 'exec /opt/maverick-mcp/bin/python -m maverick_mcp "$@"' > /usr/local/bin/maverick-mcp-server \
 && chmod 0755 /usr/local/bin/maverick-mcp-server \
 && printf '%s\n' '#!/bin/sh' 'exec /opt/acceptblue-mcp/bin/python -m acceptblue_mcp "$@"' > /usr/local/bin/acceptblue-mcp-server \
 && chmod 0755 /usr/local/bin/acceptblue-mcp-server \
 && printf '%s\n' '#!/bin/sh' 'exec /opt/authnet-mcp/bin/python -m authorizenet_mcp "$@"' > /usr/local/bin/authnet-mcp-server \
 && chmod 0755 /usr/local/bin/authnet-mcp-server \
 && mkdir -p /data && chown withhuman:withhuman /data && chmod 0700 /data \
 && mkdir -p /data/maverick-inbox && chown withhuman:withhuman /data/maverick-inbox && chmod 0700 /data/maverick-inbox
# /data holds the rotated QuickBooks refresh token (QUICKBOOKS_TOKEN_STORE_PATH);
# /data/maverick-inbox holds files the Maverick server downloads (MAVERICK_INBOX_DIR).
# Each is mounted as a named volume, which inherits this ownership on first use.
# The accept.blue and Authorize.net servers write no files, so they get no folder.
USER withhuman
