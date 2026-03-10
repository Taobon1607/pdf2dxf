web: uvicorn main:app --host 0.0.0.0 --port $PORT
worker: celery -A worker worker --loglevel=info --concurrency=1
