# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /srv/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim AS runtime
ARG APP_RELEASE=""
LABEL org.opencontainers.image.title="ods-delivery-api" \
      org.opencontainers.image.description="ODS Delivery API" \
      org.opencontainers.image.revision="${APP_RELEASE}"
# Nothing is written at run time: the root filesystem can be read-only (tmpfs on /tmp for uploads).
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_RELEASE=${APP_RELEASE}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /srv/app app
WORKDIR /srv/app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"
# uvicorn's access log is off: it prints query strings (the WebSocket token); the app logs one
# redacted line per request. Workers: WEB_CONCURRENCY (uvicorn reads it; keep 1 per pod).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
