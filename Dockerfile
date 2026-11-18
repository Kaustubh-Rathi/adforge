FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY adforge ./adforge
RUN pip install --upgrade pip && pip install ".[dashboard]"

COPY . .

EXPOSE 8000 8501 9001 9002 9003 9004 9005 9006 9007 9012

CMD ["python", "-c", "import adforge; print('AdForge image ready')"]
