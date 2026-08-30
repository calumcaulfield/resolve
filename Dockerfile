# syntax=docker/dockerfile:1

# ---- builder -------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---- runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run as a non-root user. The application never needs to write to its own
# image, so the filesystem can also be mounted read-only in production.
RUN useradd --create-home --uid 10001 resolve

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=resolve:resolve src ./src
COPY --chown=resolve:resolve evals ./evals
COPY --chown=resolve:resolve alembic.ini ./
COPY --chown=resolve:resolve alembic ./alembic

USER resolve
ENV PYTHONPATH=/app/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "resolve.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
