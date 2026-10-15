### edit as needed!

# --- Stage 1: Base Environment ---
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Stage 2: Dependencies ---
COPY config/requirements.txt ./config/
RUN pip install --no-cache-dir -r config/requirements.txt

# --- Stage 3: Source Code ---
# This copies everything EXCEPT what's in your .dockerignore
COPY . .

# Set Environment Variables
ENV PYTHONPATH="/app:/app/src" \
    ROYLIB_CONFIG="/app/config/config.yml" \
    PYTHONUNBUFFERED=1

RUN chmod +x entrypoint.sh

# --- STAGE 4 REMOVED ---
# No more COPY .netrc. Cloud Run will mount it for us at runtime.

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
