# One image: the API, serving the React UI it was built with.

# Build the UI with Node; only its static output (ui/dist) reaches the
# final image, so Node and node_modules do not.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui ./
RUN npm run build


FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOGSCOPE_DATA_DIR=/data \
    LOGSCOPE_UI_DIR=/app/ui/dist

WORKDIR /app

# Run as an unprivileged user. The data directory is created here, owned by
# that user, because a new named volume copies the ownership of the
# directory it is mounted over.
RUN useradd --create-home --uid 10001 logscope \
    && mkdir -p /data \
    && chown logscope:logscope /data

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY --from=ui /ui/dist ./ui/dist

USER logscope
EXPOSE 8000

# Liveness only (GET /api/health).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "logscope.healthcheck"]

CMD ["python", "-m", "logscope.serve"]
