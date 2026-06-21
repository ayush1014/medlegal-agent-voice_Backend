# MedLegal backend image — runs BOTH the API (uvicorn) and the voice worker
# (python -m app.agent.worker start); docker-compose picks the command per service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libgomp1 = onnxruntime (silero VAD + turn detector); build-essential for any
# source-only wheels; curl/ca-certificates for healthchecks + TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the EXACT tested versions (full closure from the working venv).
# --no-deps because the lock is a complete `pip freeze`: it already contains every
# transitive dependency pinned, and it intentionally carries an known-benign clash
# (langchain-openai 0.2.14 pins openai<2 but the app runs openai 2.x at runtime),
# which a strict re-resolve would reject. --no-deps reproduces the working venv exactly.
COPY requirements.lock.txt ./
RUN pip install --upgrade pip && pip install --no-deps -r requirements.lock.txt

COPY . .

# Default = API; the worker service overrides this in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
