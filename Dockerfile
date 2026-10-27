FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /bin/

RUN useradd -m -u 1000 user
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=user . /app
RUN chmod +x /app/entrypoint.sh \
    && python manage.py collectstatic --noinput

USER user
EXPOSE 8000
CMD ["./entrypoint.sh"]
