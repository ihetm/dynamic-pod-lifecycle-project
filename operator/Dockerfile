FROM python:3.11-slim

WORKDIR /app

# Install necessary build tools for kopf + k8s python client
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY session_operator.py .

# Run operator via Kopf
CMD ["kopf", "run", "--standalone", "/app/session_operator.py"]
