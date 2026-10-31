FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Save a copy outside /app/data so Railway volume mounts don't mask it
RUN cp data/ip_blocklist.json /app/ip_blocklist.json.bak 2>/dev/null || true

RUN chmod +x /app/run_all_catalogs.sh

CMD ["/app/run_all_catalogs.sh"]
