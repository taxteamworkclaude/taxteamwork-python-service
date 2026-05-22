# Python service with unrar binary (needed by rarfile library)
FROM python:3.11-slim

# Install unrar (required to extract .rar archives)
RUN apt-get update \
 && apt-get install -y --no-install-recommends unrar-free \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Render injects $PORT; default 8080 for local
ENV PORT=8080
EXPOSE 8080

# gunicorn for production (2 workers, 120s timeout for big files)
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 300 main:app
