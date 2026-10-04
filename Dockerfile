FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# 容器内以非 root 用户运行
RUN useradd -r -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["sh", "-c", "python -c 'from app.routes import create_app; create_app()' && exec gunicorn -w 2 -b 0.0.0.0:8000 --timeout 60 wsgi:app"]
