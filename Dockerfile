FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY world-catalog ./world-catalog
COPY world-kernel ./world-kernel
COPY src ./src
COPY packs ./packs
COPY docs ./docs

RUN pip install --no-cache-dir ".[onebot]" && useradd -m -u 1000 house && mkdir -p /app/runtime && chown -R house:house /app
USER house

EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/', timeout=3).read(1)"]
CMD ["ai-house", "up", "--host", "0.0.0.0", "--port", "8780", "--data-dir", "/app/runtime"]
