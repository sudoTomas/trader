FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv sync --no-dev

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "trader.surfaces.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
