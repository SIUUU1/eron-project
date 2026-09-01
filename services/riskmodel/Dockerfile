# services/riskmodel/Dockerfile
# ⚠ clinicalnlp 패턴을 따른다. artifacts/ 는 COPY 하지 않고 볼륨으로 마운트한다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# LightGBM 은 OpenMP 런타임(libgomp)을 dlopen 한다. python:3.12-slim 에는 없어서
# 없으면 import 단계에서 OSError: libgomp.so.1 로 죽는다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY riskmodel ./riskmodel

RUN groupadd --system --gid 10002 riskmodel \
    && useradd --system --uid 10002 --gid riskmodel --home-dir /app riskmodel \
    && chown -R riskmodel:riskmodel /app

USER riskmodel

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8790/health', timeout=3).close()"

CMD ["python", "-m", "riskmodel.service"]
