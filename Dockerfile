# Base image pinned to match the installed Playwright Python package version
# (checked locally with `python3.14 -m playwright --version` -> 1.61.0).
#
# IMPORTANT: Microsoft only publishes playwright/python images for a subset
# of versions/tags. Before deploying, verify this tag actually exists, e.g.:
#   docker manifest inspect mcr.microsoft.com/playwright/python:v1.61.0-jammy
# If it 404s, use the closest published tag <= your local playwright version
# (see https://mcr.microsoft.com/en-us/product/playwright/python/tags) and
# pin the same version in requirements.txt so pip and the base image agree.
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY bot.py .

# .env is supplied at runtime via docker-compose env_file (not baked into image)

CMD ["python", "bot.py"]
