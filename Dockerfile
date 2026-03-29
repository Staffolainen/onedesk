FROM python:3.12-slim

# Minimal system deps — ReportLab is pure Python, no native libs needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Persistent data lives outside the image
VOLUME ["/app/instance", "/app/static/uploads"]

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
