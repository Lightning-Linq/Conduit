# Conduit seed-skills webhook server.
FROM python:3.11-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app

# Install the app plus every skill's optional dependency so all 15 skills load.
RUN pip install --no-cache-dir ".[all]"

ENV REQUIRE_PAYMENT_PROOF=true
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
