# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY tests/fixtures/hyperliquid/ /build/tests/fixtures/hyperliquid/
RUN npm run test:unit && npm run build

FROM rust:1.94-slim-bookworm AS rust-build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/target,sharing=locked \
    cargo build --release --locked \
    && cp /build/target/release/hl-trader /tmp/hl-trader

FROM python:3.12-slim-bookworm AS web
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN python -m pip install -r /app/backend/requirements.lock
COPY backend/ /app/backend/
COPY tests/fixtures/ /app/tests/fixtures/
RUN python -m pip install --no-deps /app/backend
COPY --from=rust-build /tmp/hl-trader /usr/local/bin/hl-trader
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin hlapp \
    && install -d -o hlapp -g hlapp /var/lib/hl-trader /var/lib/hl-wallet-keys
USER 10001
EXPOSE 8000
CMD ["python", "-m", "hl_app"]

FROM nginx:1.27-alpine AS gateway
COPY --from=frontend-build /build/frontend/dist/ /usr/share/nginx/html/
COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
