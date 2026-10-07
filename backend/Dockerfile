# api image: Python only. LLM calls happen in the worker image, never in the request path.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 jobfinder
WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY jobfinder ./jobfinder
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --no-dev

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /data/store /data/claude \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R jobfinder:jobfinder /app /data

USER jobfinder
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
# Call uvicorn directly: `uv run` would try to re-sync as a non-root user and fail.
CMD ["uvicorn", "jobfinder.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
