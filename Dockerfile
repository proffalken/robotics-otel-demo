FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.14-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app
USER app
ENTRYPOINT ["/app/.venv/bin/fleet-sim"]
