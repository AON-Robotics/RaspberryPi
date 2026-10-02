# Web chat app image. Build context is bridge/ (see ../compose.yml), so both
# agent/ (the shared loop) and web/ can be copied in.
FROM python:3.12-slim

WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

COPY agent/ agent/
COPY web/ web/

WORKDIR /app/web
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
