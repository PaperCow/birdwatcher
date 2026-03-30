FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.13-slim-bookworm

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/

RUN useradd -r -s /bin/false appuser
USER appuser

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
