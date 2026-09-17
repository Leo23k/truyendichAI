FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY infra/tts/requirements.txt ./requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY infra/tts/service.py ./service.py
RUN mkdir -p /app/data/tts_cache

EXPOSE 8002
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8002"]
