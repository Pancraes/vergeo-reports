# API image. No secrets are baked in: keys and DATABASE_URL arrive as environment variables.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilt when requirements.txt changes,
# not on every code edit.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# Run as an unprivileged user, not root.
RUN useradd --create-home --uid 10001 vergeo
USER vergeo

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Apply migrations, then serve. Fine for one instance; with several replicas, run
# `alembic upgrade head` once as a separate release step instead (see README).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
