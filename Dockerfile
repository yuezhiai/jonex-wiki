FROM ghcr.io/astral-sh/uv:0.11.21 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache && rm -rf /root/.cache/uv
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8007
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8007/healthz')" || exit 1
CMD ["sh", "-c", "alembic upgrade head && jonex-wiki-api"]
