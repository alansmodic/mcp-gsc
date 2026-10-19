# Remote (Railway) image: upstream server + multi-account/auth wrapper.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-cache --no-install-project

COPY gsc_server.py ./
COPY remote/server.py remote/server.py

ENV PYTHONUNBUFFERED=1
CMD ["uv", "run", "--no-sync", "python", "remote/server.py"]
