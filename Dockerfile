FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

# Use the image's interpreter; never download one at build time.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# curl is only for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first, so code edits don't invalidate it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && mkdir /data && chown app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    EXCHANGE_DB=/data/exchange.db

EXPOSE 8000
CMD ["uvicorn", "exchange.api:app", "--host", "0.0.0.0", "--port", "8000"]
