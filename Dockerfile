FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway inject PORT env var
CMD uvicorn main_local_pymupdf:app --host 0.0.0.0 --port $PORT
