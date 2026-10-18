FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libraqm нужен Pillow для сложных эмодзи-последовательностей.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libraqm0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py generate_samples.py ./
COPY app ./app
COPY config ./config
COPY assets ./assets
COPY templates ./templates

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/.cache \
    && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
