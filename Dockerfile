FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RECEIPT_SERVICE_HOST=0.0.0.0 \
    RECEIPT_SERVICE_PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-ocr-service.txt ./requirements-ocr-service.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements-ocr-service.txt

COPY receipt_service.py ./receipt_service.py

EXPOSE 8080

CMD ["python", "receipt_service.py"]
