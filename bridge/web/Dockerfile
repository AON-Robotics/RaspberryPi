# Web chat app image. Build context is bridge/ (see ../compose.yml), so both
# agent/ (the shared loop) and web/ can be copied in. ../.dockerignore keeps
# .env and the local venvs out of the image.
FROM python:3.12-slim-bookworm

# No .pyc files (the container filesystem is read-only) and unbuffered logs,
# so audit lines show up in `docker compose logs` right away.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

COPY agent/ agent/
COPY web/ web/

# Run as an unprivileged user, never root.
RUN useradd --system --uid 10001 --no-create-home app
USER app

WORKDIR /app/web
EXPOSE 8080
# Marks the container unhealthy if the app stops answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health/live', timeout=3)"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", \
     "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
