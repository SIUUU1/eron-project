FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY clinicalnlp_api3 ./clinicalnlp_api3
COPY contracts ./contracts
COPY docs ./docs
COPY prompts ./prompts
COPY scripts ./scripts
COPY contract-manifest.json ./contract-manifest.json

RUN groupadd --system --gid 10001 clinicalnlp \
    && useradd --system --uid 10001 --gid clinicalnlp --home-dir /app clinicalnlp \
    && chown -R clinicalnlp:clinicalnlp /app

USER clinicalnlp

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/ready', timeout=3).close()"

CMD ["python", "-m", "clinicalnlp_api3.service"]
