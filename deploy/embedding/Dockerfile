# 小屋记忆用的本地向量服务：fastembed + BAAI/bge-small-zh-v1.5（CPU）。
# 库的版本和模型都固定：记忆代码里的回想门槛、出站检查门槛是对着这一组合调的，换一个分数的刻度就不一样。
# 模型文件不打进镜像：第一次用到时下载进 /models（挂一个卷），之后不再联网。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EMBEDDING_CACHE=/models \
    EMBEDDING_HOST=0.0.0.0 \
    EMBEDDING_PORT=7998 \
    HF_HOME=/models/.hf

RUN pip install --no-cache-dir fastembed==0.7.3 \
 && useradd -m -u 1000 embed \
 && mkdir -p /models \
 && chown embed:embed /models

WORKDIR /app
COPY server.py /app/server.py
USER embed

EXPOSE 7998
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7998/health', timeout=3).read(1)"]
CMD ["python", "/app/server.py"]
