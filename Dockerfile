FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY icloud_gateway ./icloud_gateway

RUN python -m pip install . \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=8s --start-period=10s --retries=8 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()"]

CMD ["icloud-code-gateway"]
