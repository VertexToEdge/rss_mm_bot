FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev

COPY main.py .

CMD ["uv", "run", "python", "main.py"]
