# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md alembic.ini ./
COPY traceloom ./traceloom
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 traceloom
COPY --from=build /install /usr/local
WORKDIR /app
COPY alembic.ini ./
RUN mkdir -p /data && chown traceloom /data
USER traceloom
ENV TRACELOOM_BLOB_DIR=/data/blobs
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/public/health', timeout=2)"
CMD ["traceloom", "serve", "--host", "0.0.0.0", "--port", "8000"]
