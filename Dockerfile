# Node for the Codex CLI comes from the official Node image, not from
# NodeSource's install script piped into bash. Only node and Codex are copied
# into the final image; npm stays behind, so its bundled dependencies (old
# pacote, sigstore and the like that Trivy flags) never reach it.
FROM node:22-trixie-slim AS codex
# Pinned to match the host version that created auth.json
RUN npm install -g @openai/codex@0.162.1

# Floating tag on purpose: the monthly docker-auto-update.sh run rebuilds with
# --pull, so Debian and Python fixes arrive. Until 2026-10-25 this was pinned
# to a digest, which froze the OS packages.
FROM python:3.12-slim

WORKDIR /app

# bubblewrap is Codex's sandbox
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

# Exact versions with hashes; see requirements.in to update them
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Debian security updates between base image releases. SECURITY_REFRESH is
# set by docker-auto-update.sh, so this layer reruns when fixes are waiting.
ARG SECURITY_REFRESH=manual
RUN echo "security refresh: ${SECURITY_REFRESH}" \
    && apt-get update && apt-get upgrade -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY workbot ./workbot

ENV PYTHONUNBUFFERED=1
ENV WORKBOT_DB_PATH=/data/workbot.db
ENV HOME=/tmp
ENV CODEX_HOME=/app/.codex-home

# Run unprivileged. uid 1000 matches the host owner of ./data, so the mounted
# volume (SQLite db + strings) stays writable.
USER 1000:1000

CMD ["python", "-m", "workbot.app"]
