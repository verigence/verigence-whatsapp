FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn wa_service.main:app --host :: --port ${PORT:-8000} --timeout-graceful-shutdown 20 --max-requests 2000 --max-requests-jitter 200"]
