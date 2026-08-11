FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ffmpeg ca-certificates libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

RUN pip install --upgrade pip setuptools wheel \
    && pip install "qwen-asr[vllm]==0.0.6" fastapi==0.128.2 "uvicorn[standard]==0.48.0" numpy==2.2.6

RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/qwen_asr/server.py /app/server.py
EXPOSE 8101
CMD ["python", "server.py"]
