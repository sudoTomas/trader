# Published as linux/amd64 and linux/arm64. uv is pinned to the toolchain that
# wrote uv.lock; both base images are multi-arch.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "trader.surfaces.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
