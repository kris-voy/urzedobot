FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ARG PLAYWRIGHT_VERSION=1.61.0
ENV PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/sv.db

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import importlib.metadata as m; assert m.version('playwright') == '${PLAYWRIGHT_VERSION}'" \
    && mkdir -p /app/data \
    && chown -R pwuser:pwuser /app

COPY --chown=pwuser:pwuser *.py ./
USER pwuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "bot.py"]
