# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY runlatch ./runlatch
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --upgrade pip && pip install ".[server]"

RUN useradd --create-home --shell /bin/bash runlatch && \
    chown -R runlatch:runlatch /app
USER runlatch

EXPOSE 8000
CMD ["uvicorn", "runlatch.runtime.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
