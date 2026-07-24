FROM python:3.11-slim

WORKDIR /app

# Install system deps for asyncpg and grpc
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the whole project before installing: hatchling validates the readme and
# license files referenced in pyproject.toml (and needs src/conduit to build),
# so pyproject.toml alone is not enough to generate metadata.
COPY . .

# Install the package (editable, with dev extras — mirrors the local setup).
RUN pip install --no-cache-dir -e ".[dev]"

# Run DB migrations on container start (entrypoint), then exec the CMD below.
RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]

EXPOSE 8000

CMD ["uvicorn", "conduit.main:app", "--host", "0.0.0.0", "--port", "8000"]
