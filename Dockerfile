FROM python:3.11-slim

WORKDIR /app

# Install system deps for asyncpg and grpc
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy application code
COPY . .

# Install the package
RUN pip install --no-cache-dir -e .

# Run DB migrations on container start (entrypoint), then exec the CMD below.
RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 8000

CMD ["uvicorn", "conduit.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
