# withHuman gateway tunnel + Intuit QuickBooks Online MCP server (stdio).
# The tunnel runs `qbo-mcp-server` as a child process (see tunnel.yaml).
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

# ---- runtime stage: the unchanged tunnel image + the server ----
FROM ${TUNNEL_IMAGE}
USER root
COPY --from=build /src /opt/qbo-mcp
RUN ln -s /opt/qbo-mcp/dist/index.js /usr/local/bin/qbo-mcp-server \
 && mkdir -p /data && chown withhuman:withhuman /data && chmod 0700 /data
# /data holds the rotated QuickBooks refresh token (QUICKBOOKS_TOKEN_STORE_PATH);
# it is mounted as a named volume, which inherits this ownership on first use.
USER withhuman
