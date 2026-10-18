FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    STATE_DIR=/state WORK_ROOT=/work SOURCE_ROOT=/original \
    PUBLICATION_ROOT=/publication STORAGE_GUARD_DIR=/storage-guard \
    STATIC_DIR=/app/frontend/dist
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libebur128-1 libchromaprint-tools ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 phonoteka && useradd -u 10001 -g phonoteka -M phonoteka
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY backend/ ./backend/
RUN pip install -c requirements.lock .
COPY --from=frontend /build/dist ./frontend/dist
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["uvicorn", "phonoteka.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
