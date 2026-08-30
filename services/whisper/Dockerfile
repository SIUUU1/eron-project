FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN groupadd --system --gid 10002 whisper \
    && useradd --system --uid 10002 --gid whisper --home-dir /app whisper \
    && mkdir -p /runtime/state/outputs /runtime/state/spool \
    && chown -R whisper:whisper /app /runtime/state

USER whisper

EXPOSE 8780

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8780/v1/health', timeout=3).close()"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8780", "--no-access-log"]
