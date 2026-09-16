FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY infra/translator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/translator/service.py ./service.py

EXPOSE 7861
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "7861"]
