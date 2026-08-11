FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY gateway/requirements.txt /app/gateway-requirements.txt
COPY benchmark/requirements-optional.txt /app/benchmark-requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/gateway-requirements.txt -r /app/benchmark-requirements.txt
COPY gateway /app/gateway
COPY benchmark /app/benchmark
CMD ["python", "benchmark/benchmark_stt.py", "--help"]
