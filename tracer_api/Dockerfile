FROM python:3.11-slim

WORKDIR /app

# System dependencies for netmiko / paramiko / cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY tracer_api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# History DB lives in a persistent volume mount point
VOLUME ["/data"]

ENV TRACER_HISTORY_DB_PATH=/data/trace_history.db \
    TRACER_HOST=0.0.0.0 \
    TRACER_PORT=8000

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "tracer_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
