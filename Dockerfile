FROM node:22-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb AS node

FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3

LABEL org.opencontainers.image.source="https://github.com/CopyPasteFail/articles-rss-to-podcast"

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WRANGLER_SEND_METRICS=false \
    PATH="/opt/podcast/node_modules/.bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg bash git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

WORKDIR /opt/podcast
COPY requirements.txt package.json package-lock.json ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check
RUN npm ci --no-audit --no-fund \
    && npm cache clean --force

WORKDIR /workspace
