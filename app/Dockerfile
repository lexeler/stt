FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

# CPU-only torch saves ~2GB compared to the CUDA wheel
RUN pip install --upgrade pip \
 && pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.10.0 torchaudio==2.10.0

COPY --from=gigaam . /opt/GigaAM
RUN pip install /opt/GigaAM[longform]

# NB: paths are relative to the docker-compose `context: .` (= project root),
# not to this Dockerfile. So we prefix with `app/`.
COPY app/requirements.txt /opt/app/requirements.txt
RUN pip install -r /opt/app/requirements.txt

COPY app/server /opt/app/server

RUN mkdir -p /opt/app/data/uploads /opt/app/data/transcripts

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
