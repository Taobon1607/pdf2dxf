FROM python:3.11-slim

# System deps for OpenCV + Tesseract
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create temp directories
RUN mkdir -p tmp/uploads tmp/outputs data

# Default: run web server
# Override CMD in Railway to start Celery worker separately
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
