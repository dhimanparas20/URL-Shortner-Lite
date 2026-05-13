# Use the official Python slim image (Debian-based, glibc)
FROM python:3.14-slim

# Set environment variables
ARG TZ=Asia/Kolkata
ARG COMPOSE_BAKE=true

ENV TZ=${TZ} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COMPOSE_BAKE=true \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=90 \
    UV_NO_PROGRESS=1 \
    UV_CONCURRENT_DOWNLOADS=15 \
    GUNICORN_WORKERS=4 \
    GUNICORN_RELOAD=false

# Copy the uv and uvx binaries from the official uv image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy only dependency files first for better Docker cache utilization
COPY pyproject.toml uv.lock ./

# Install dependencies with BuildKit cache mount for uv cache
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=90 uv sync --frozen --no-dev --no-install-project

# Copy the entire application code into the container
COPY . .

# Install the project itself (deps already cached from above)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --start-interval=2s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

CMD ["sh", "-c", "\
    uv run gunicorn \
        -w ${GUNICORN_WORKERS} \
        -b 0.0.0.0:5000 \
        --timeout 120 \
        --graceful-timeout 30 \
        --worker-tmp-dir /dev/shm \
        --access-logfile - \
        --error-logfile - \
        ${GUNICORN_RELOAD:+--reload} \
        app:app \
"]