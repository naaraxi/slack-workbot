# Pinned to a digest so rebuilds are reproducible (see project_docker_image_pinning).
# To bump: docker pull python:3.12-slim && docker images --digests python
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codex CLI (Node-based) for the Jira task-suggestion step. Pinned to match the
# host version that created auth.json. Kept before the app copy so code changes
# don't rebuild this heavy layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs bubblewrap \
    && npm install -g @openai/codex@0.162.1 \
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
