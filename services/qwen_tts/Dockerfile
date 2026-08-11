FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ffmpeg ca-certificates libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

RUN pip install --upgrade pip setuptools wheel \
    && pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchaudio==2.8.0 \
    && pip install fastapi==0.128.2 "uvicorn[standard]==0.48.0" python-multipart==0.0.21 numpy==2.3.5 \
       qwen-tts==0.1.1 faster-qwen3-tts==0.3.2

WORKDIR /app
COPY services/qwen_tts/server.py /app/server.py
EXPOSE 8102
CMD ["python", "server.py"]
