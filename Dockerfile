# Fargate image for PDF-heavy extraction (step 6) and local development.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
ENV PYTHONPATH=/app/src

EXPOSE 8000
CMD ["uvicorn", "trustsight.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
