# Multi-arch image for the SONOFF iHost (linux/arm/v7) and other hosts.
# Build locally:  docker buildx build --platform linux/arm/v7 -t <you>/ihost-bridge .
# The GitHub workflow in .github/workflows/docker.yml builds and pushes arm/v7, arm64 and amd64.

FROM python:3.12-slim AS builder
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY bridge ./bridge
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="ihost-bridge" \
      org.opencontainers.image.description="Bridge Midea and other appliances into a SONOFF iHost / eWeLink CUBE" \
      org.opencontainers.image.source="https://github.com/OWNER/ihost-bridge"
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

ENV BRIDGE_DATA_DIR=/data \
    BRIDGE_PORT=9900 \
    CUBE_HOST=ihost \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 9900

CMD ["python", "-m", "bridge"]
