FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN uv sync --frozen --no-dev && useradd --create-home --uid 1000 morrow \
    && mkdir -p /var/lib/morrow && chown morrow:morrow /var/lib/morrow
ENV PATH="/app/.venv/bin:$PATH"
USER morrow
EXPOSE 8200
CMD ["uvicorn", "morrow_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8200", "--no-access-log"]
